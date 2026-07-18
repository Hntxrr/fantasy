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
    SignupError,
    SubmissionError,
)
from .crypto import CredentialCipher
from .models import SubmissionLog
from .repository import Repository
from .signup import SignupResult, build_profile

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
        post_submit_dwell: float = 0.5,
    ) -> None:
        self.concurrency = max(1, min(15, concurrency))
        self.headless = headless
        self.launch_stagger = max(0.0, launch_stagger)
        self.proxies = [p for p in (proxies or []) if p.strip()]
        self.login_timeout = login_timeout
        self.submit_retries = max(1, submit_retries)
        self.retry_delay = retry_delay
        # Seconds to keep the (visible) browser open after a successful submit
        # so you can actually see the confirmation before it closes.
        self.post_submit_dwell = max(0.0, post_submit_dwell)

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
            # Linger on the confirmation so it's visibly submitted (visible mode only).
            if self.post_submit_dwell > 0 and not self.headless:
                status(f"Submitted & confirmed - closing in {self.post_submit_dwell:g}s...")
                time.sleep(self.post_submit_dwell)
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



# --------------------------------------------------------------------------- #
# Sign up runner
# --------------------------------------------------------------------------- #
@dataclass
class SignupCallbacks:
    on_task_start: Callable[[str], None] = _noop           # email
    on_status: Callable[[str, str], None] = _noop          # email, message
    on_result: Callable[[SignupResult], None] = _noop
    on_progress: Callable[[int, int], None] = _noop        # done, total
    should_cancel: Callable[[], bool] = field(default=lambda: False)


class SignupRunner:
    """Register many new accounts concurrently from a list of emails.

    For each email: fabricate a random identity, create the local account row
    (which allocates an isolated Chrome profile), drive the site's registration
    form in that profile, and -- on success -- mark the session valid so the
    account lands in Accounts already logged in. On failure the just-created
    row is removed so only completed signups remain. Emails that already exist
    as accounts are skipped.
    """

    def __init__(
        self,
        *,
        city: str = "",
        state: str = "",
        postal_code: str = "",
        country: str = "United States",
        concurrency: int = 1,
        headless: bool = False,
        launch_stagger: float = 6.0,
        proxies: Optional[list[str]] = None,
        signup_timeout: int = 45,
        post_submit_dwell: float = 4.0,
        submit_attempts: int = 8,
        assist: bool = True,
        assist_timeout: int = 600,
    ) -> None:
        self.city = city
        self.state = state
        self.postal_code = postal_code
        self.country = country or "United States"
        self.concurrency = max(1, min(15, concurrency))
        self.headless = headless
        self.launch_stagger = max(0.0, launch_stagger)
        self.proxies = [p for p in (proxies or []) if p.strip()]
        self.signup_timeout = signup_timeout
        self.post_submit_dwell = max(0.0, post_submit_dwell)
        self.submit_attempts = max(1, submit_attempts)
        # Assist mode: fill the form, then the user clicks Submit + clears the
        # reCAPTCHA and confirms via the in-browser panel (default; the site's
        # Submit is a reCAPTCHA button so full automation can't clear it).
        self.assist = assist
        self.assist_timeout = max(30, assist_timeout)

        self._cipher = CredentialCipher()
        self._launch_lock = threading.Lock()
        self._last_launch = 0.0
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _stagger(self) -> None:
        if self.launch_stagger <= 0:
            return
        with self._launch_lock:
            now = time.monotonic()
            wait = self._last_launch + self.launch_stagger - now
            if wait > 0:
                time.sleep(wait)
            self._last_launch = time.monotonic()

    def run(
        self,
        emails: list[str],
        callbacks: Optional[SignupCallbacks] = None,
    ) -> list[SignupResult]:
        cb = callbacks or SignupCallbacks()
        total = len(emails)
        results: list[SignupResult] = []
        done = 0
        should_cancel = lambda: self._cancel.is_set() or cb.should_cancel()  # noqa: E731

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            future_map = {
                pool.submit(self._signup_one, idx, email, cb, should_cancel): email
                for idx, email in enumerate(emails)
            }
            for future in as_completed(future_map):
                res = future.result()
                results.append(res)
                cb.on_result(res)
                done += 1
                cb.on_progress(done, total)
        return results

    def _signup_one(
        self,
        index: int,
        email: str,
        cb: SignupCallbacks,
        should_cancel: Callable[[], bool],
    ) -> SignupResult:
        email = (email or "").strip()
        status = lambda m: cb.on_status(email, m)  # noqa: E731

        if should_cancel():
            return SignupResult(email, False, "Cancelled before start.")
        cb.on_task_start(email)

        repo = Repository(cipher=self._cipher)
        account = None
        try:
            if repo.email_exists(email):
                return SignupResult(email, False, "Skipped - already an account.")

            profile = build_profile(
                email,
                city=self.city, state=self.state,
                postal_code=self.postal_code, country=self.country,
            )
            label = email.split("@", 1)[0]
            account = repo.add_account(label, email, profile.password)

            proxy = self.proxies[index % len(self.proxies)] if self.proxies else None
            self._stagger()
            if should_cancel():
                repo.delete_account(account.id)
                return SignupResult(email, False, "Cancelled.")

            # Assist mode forces a visible browser (the user interacts with it).
            headless = self.headless and not self.assist
            with automation.chrome_session(
                account.profile_dir, headless=headless, proxy=proxy
            ) as driver:
                if self.assist:
                    automation.assist_signup(
                        driver, profile, status_cb=status,
                        timeout=self.signup_timeout,
                        wait_timeout=self.assist_timeout,
                    )
                else:
                    automation.do_signup(
                        driver, profile, status_cb=status,
                        timeout=self.signup_timeout,
                        post_submit_dwell=self.post_submit_dwell,
                        submit_attempts=self.submit_attempts,
                    )

            # Registered and saved to Accounts, but left as "not signed in" so
            # it appears at the BOTTOM of the list until a real login/pick run
            # confirms the session (per user preference). The Chrome profile
            # still holds whatever session the signup created.
            status("Registered - added to Accounts (not signed in).")
            return SignupResult(
                email, True, "Registered - added to Accounts (not signed in).",
                password=profile.password, account_id=account.id,
            )
        except SignupError as exc:
            if account is not None:
                repo.delete_account(account.id)
            return SignupResult(email, False, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected signup error for %s", email)
            if account is not None:
                repo.delete_account(account.id)
            return SignupResult(email, False, f"Unexpected error: {exc}")
        finally:
            repo.close()



# --------------------------------------------------------------------------- #
# Assisted sign-in runner
# --------------------------------------------------------------------------- #
@dataclass
class SigninCallbacks:
    on_task_start: Callable[[int], None] = _noop           # account_id
    on_status: Callable[[int, str], None] = _noop          # account_id, message
    on_result: Callable[[int, bool, str], None] = _noop    # account_id, ok, message
    on_progress: Callable[[int, int], None] = _noop        # done, total
    should_cancel: Callable[[], bool] = field(default=lambda: False)


class SigninRunner:
    """Assisted login for existing accounts.

    Opens each account's browser, fills email+password, and lets you click
    Log In + clear the reCAPTCHA. On success (auto-detected or you confirm) the
    account is marked session-valid and the session is saved in its profile, so
    later pick runs reuse it. A visible browser is always used.
    """

    def __init__(
        self,
        account_ids: list[int],
        *,
        concurrency: int = 2,
        launch_stagger: float = 3.0,
        proxies: Optional[list[str]] = None,
        wait_timeout: int = 600,
    ) -> None:
        self.account_ids = list(account_ids)
        self.concurrency = max(1, min(15, concurrency))
        self.launch_stagger = max(0.0, launch_stagger)
        self.proxies = [p for p in (proxies or []) if p.strip()]
        self.wait_timeout = max(30, wait_timeout)

        self._cipher = CredentialCipher()
        self._launch_lock = threading.Lock()
        self._last_launch = 0.0
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _stagger(self) -> None:
        if self.launch_stagger <= 0:
            return
        with self._launch_lock:
            now = time.monotonic()
            wait = self._last_launch + self.launch_stagger - now
            if wait > 0:
                time.sleep(wait)
            self._last_launch = time.monotonic()

    def run(self, callbacks: Optional[SigninCallbacks] = None) -> None:
        cb = callbacks or SigninCallbacks()
        total = len(self.account_ids)
        done = 0
        should_cancel = lambda: self._cancel.is_set() or cb.should_cancel()  # noqa: E731
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._signin_one, idx, aid, cb, should_cancel): aid
                for idx, aid in enumerate(self.account_ids)
            }
            for future in as_completed(futures):
                future.result()
                done += 1
                cb.on_progress(done, total)

    def _signin_one(self, index, account_id, cb, should_cancel) -> None:
        status = lambda m: cb.on_status(account_id, m)  # noqa: E731
        if should_cancel():
            cb.on_result(account_id, False, "Cancelled.")
            return
        cb.on_task_start(account_id)
        repo = Repository(cipher=self._cipher)
        try:
            acc = repo.get_account(account_id, include_password=True)
            if acc is None:
                cb.on_result(account_id, False, "Account no longer exists.")
                return
            proxy = self.proxies[index % len(self.proxies)] if self.proxies else None
            self._stagger()
            if should_cancel():
                cb.on_result(account_id, False, "Cancelled.")
                return
            with automation.chrome_session(acc.profile_dir, headless=False, proxy=proxy) as driver:
                automation.assist_login(
                    driver, acc.email, acc.password,
                    status_cb=status, wait_timeout=self.wait_timeout,
                )
            repo.set_session_valid(account_id, True)
            cb.on_result(account_id, True, "Logged in - session saved.")
        except Exception as exc:  # noqa: BLE001
            cb.on_result(account_id, False, str(exc))
        finally:
            repo.close()



# --------------------------------------------------------------------------- #
# Login-status verifier (captcha-free)
# --------------------------------------------------------------------------- #
class VerifyRunner:
    """Check which accounts are already logged in and update session_valid.

    Opens each account's profile headlessly and checks for a live session (no
    login attempt, so no captcha). Marks accounts valid/invalid accordingly.
    """

    def __init__(
        self,
        account_ids: list[int],
        *,
        concurrency: int = 4,
        headless: bool = True,
        proxies: Optional[list[str]] = None,
        fast: bool = True,
    ) -> None:
        self.account_ids = list(account_ids)
        self.concurrency = max(1, min(15, concurrency))
        self.headless = headless
        self.proxies = [p for p in (proxies or []) if p.strip()]
        # Fast mode reads the profile cookie DB directly (no browser launch).
        self.fast = fast
        self._cipher = CredentialCipher()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, callbacks: Optional[SigninCallbacks] = None) -> None:
        cb = callbacks or SigninCallbacks()
        total = len(self.account_ids)
        done = 0
        should_cancel = lambda: self._cancel.is_set() or cb.should_cancel()  # noqa: E731
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._verify_one, idx, aid, cb, should_cancel): aid
                for idx, aid in enumerate(self.account_ids)
            }
            for future in as_completed(futures):
                future.result()
                done += 1
                cb.on_progress(done, total)

    def _verify_one(self, index, account_id, cb, should_cancel) -> None:
        if should_cancel():
            return
        cb.on_task_start(account_id)
        repo = Repository(cipher=self._cipher)
        try:
            acc = repo.get_account(account_id, include_password=False)
            if acc is None:
                return
            proxy = self.proxies[index % len(self.proxies)] if self.proxies else None
            state = "unknown"
            try:
                if self.fast:
                    state = automation.profile_has_session(acc.profile_dir)
                else:
                    state = automation.check_login_state(acc.profile_dir, headless=self.headless, proxy=proxy)
            except Exception as exc:  # noqa: BLE001
                cb.on_status(account_id, f"check failed: {exc}")
            # Only change the flag on a CLEAR signal; leave it untouched when
            # 'unknown' so we never wipe a good session (e.g. page didn't load).
            if state == "in":
                repo.set_session_valid(account_id, True)
                cb.on_result(account_id, True, "Logged in")
            elif state == "out":
                repo.set_session_valid(account_id, False)
                cb.on_result(account_id, False, "Not logged in")
            else:
                cb.on_result(account_id, False, "Unknown - left unchanged")
        finally:
            repo.close()
