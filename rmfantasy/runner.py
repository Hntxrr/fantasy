"""Concurrent submission runner.

Executes an :class:`~rmfantasy.assignment.AssignmentPlan` across many accounts
using a thread pool (1-15 concurrent browsers). Each task:

    build isolated Chrome  ->  ensure logged in (reuse session or auto-login)
    ->  load picks page  ->  select core five + wildcard  ->  submit  ->  verify
    ->  log result  ->  close browser

Features:
  * Adjustable concurrency.
  * Launch stagger so browsers don't all hit the site at once (rate-limit
    protection).
  * Optional round-robin HTTP proxies.
  * Transient-failure retries (login hiccups, slow loads).
  * Cooperative cancellation (Stop button).
  * Per-thread SQLite connection (connections are not shareable across threads);
    a shared cipher avoids repeated keyring reads.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import automation
from .assignment import RoundAssignment
from .automation import (
    AutomationError,
    EligibilityError,
    LoginRequired,
    PickRequest,
    SubmissionError,
)
from .crypto import CredentialCipher
from .models import SubmissionLog
from .repository import Repository

log = logging.getLogger(__name__)


def _noop(*_a, **_k):
    return None


@dataclass
class RunResult:
    account_id: int
    account_label: str
    lineup_index: int
    wildcard: str
    success: bool
    message: str


@dataclass
class RunCallbacks:
    on_task_start: Callable[[RoundAssignment], None] = _noop
    on_status: Callable[[int, str], None] = _noop          # account_id, message
    on_result: Callable[[RunResult], None] = _noop
    on_progress: Callable[[int, int], None] = _noop        # done, total
    should_cancel: Callable[[], bool] = field(default=lambda: False)


class ConcurrentRunner:
    def __init__(
        self,
        concurrency: int = 10,
        headless: bool = False,
        launch_stagger: float = 1.0,
        proxies: Optional[list[str]] = None,
        login_timeout: int = 30,
        submit_retries: int = 2,
        retry_delay: float = 3.0,
    ) -> None:
        self.concurrency = max(1, min(15, concurrency))
        self.headless = headless
        self.launch_stagger = max(0.0, launch_stagger)
        self.proxies = [p for p in (proxies or []) if p.strip()]
        self.login_timeout = login_timeout
        self.submit_retries = max(1, submit_retries)
        self.retry_delay = retry_delay

        self._cipher = CredentialCipher()  # shared; Fernet ops are stateless
        self._launch_lock = threading.Lock()
        self._last_launch = 0.0
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel.set()

    def _stagger(self) -> None:
        """Space out browser launches by ``launch_stagger`` seconds."""
        if self.launch_stagger <= 0:
            return
        with self._launch_lock:
            now = time.monotonic()
            wait = self._last_launch + self.launch_stagger - now
            if wait > 0:
                time.sleep(wait)
            self._last_launch = time.monotonic()

    # ------------------------------------------------------------------ #
    def run(
        self,
        assignments: list[RoundAssignment],
        callbacks: Optional[RunCallbacks] = None,
    ) -> list[RunResult]:
        cb = callbacks or RunCallbacks()
        total = len(assignments)
        results: list[RunResult] = []
        done = 0

        should_cancel = lambda: self._cancel.is_set() or cb.should_cancel()  # noqa: E731

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            future_map = {
                pool.submit(self._run_one, idx, a, cb, should_cancel): a
                for idx, a in enumerate(assignments)
            }
            for future in as_completed(future_map):
                res = future.result()
                results.append(res)
                cb.on_result(res)
                done += 1
                cb.on_progress(done, total)
        return results

    # ------------------------------------------------------------------ #
    def _run_one(
        self,
        index: int,
        assignment: RoundAssignment,
        cb: RunCallbacks,
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        acc_id = assignment.account_id
        status = lambda m: cb.on_status(acc_id, m)  # noqa: E731

        if should_cancel():
            return self._finish(assignment, False, "Cancelled before start.")

        cb.on_task_start(assignment)

        # Own DB connection for this thread; shared cipher.
        repo = Repository(cipher=self._cipher)
        try:
            account = repo.get_account(acc_id, include_password=True)
            if account is None:
                return self._finish(assignment, False, "Account no longer exists.", repo)

            proxy = None
            if self.proxies:
                proxy = self.proxies[index % len(self.proxies)]

            self._stagger()
            if should_cancel():
                return self._finish(assignment, False, "Cancelled.", repo)

            request = PickRequest(
                core_five=list(assignment.core_five),
                wildcard=assignment.wildcard,
            )

            last_error: Optional[Exception] = None
            for attempt in range(1, self.submit_retries + 1):
                if should_cancel():
                    return self._finish(assignment, False, "Cancelled.", repo)
                try:
                    return self._attempt(account, request, assignment, repo, status, proxy)
                except EligibilityError as exc:
                    # Permanent for this round -- do not retry.
                    return self._finish(assignment, False, str(exc), repo)
                except (LoginRequired, SubmissionError, AutomationError) as exc:
                    last_error = exc
                    status(f"Attempt {attempt}/{self.submit_retries} failed: {exc}")
                    if attempt < self.submit_retries:
                        time.sleep(self.retry_delay)
            return self._finish(
                assignment, False, f"Failed after {self.submit_retries} attempts: {last_error}", repo
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error for account %s", assignment.account_label)
            return self._finish(assignment, False, f"Unexpected error: {exc}", repo)
        finally:
            repo.close()

    # ------------------------------------------------------------------ #
    def _attempt(self, account, request, assignment, repo, status, proxy=None) -> RunResult:
        with automation.chrome_session(
            account.profile_dir, headless=self.headless, proxy=proxy,
        ) as driver:
            automation.ensure_logged_in(
                driver, account.email, account.password,
                login_timeout=self.login_timeout, status_cb=status,
            )
            repo.set_session_valid(account.id, True)
            round_label = automation.get_round_label(driver)
            driver.get(automation.config.BASE_URL)
            automation.submit_picks(driver, request, status_cb=status, verify=True)
        return self._finish(
            assignment, True, "Picks submitted and confirmed.", repo, round_label=round_label
        )

    # ------------------------------------------------------------------ #
    def _finish(
        self, assignment, success, message, repo: Optional[Repository] = None,
        round_label: str = "Current round",
    ) -> RunResult:
        if repo is not None:
            entry = SubmissionLog(
                id=None,
                account_label=assignment.account_label,
                account_email=assignment.account_email,
                lineup_name=f"Lineup {assignment.lineup_index}",
                core_five=", ".join(assignment.core_five),
                wildcard=assignment.wildcard,
                round_number=assignment.lineup_index,
                round_label=round_label,
                success=success,
                message=message,
            )
            try:
                repo.add_submission_log(entry)
            except Exception:  # noqa: BLE001
                log.exception("Failed to write submission log")
        level = logging.INFO if success else logging.WARNING
        log.log(level, "[%s] L%d + %s: %s",
                assignment.account_label, assignment.lineup_index, assignment.wildcard, message)
        return RunResult(
            account_id=assignment.account_id,
            account_label=assignment.account_label,
            lineup_index=assignment.lineup_index,
            wildcard=assignment.wildcard,
            success=success,
            message=message,
        )
