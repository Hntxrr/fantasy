"""Data-access layer: CRUD for accounts, lineups, wildcards, assignments,
rotation state and submission logs.

The repository owns the SQLite connection and the credential cipher, so the
rest of the app never touches SQL or encryption directly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, database
from .crypto import CredentialCipher
from .resolver import normalize_query
from .models import (
    Account,
    Assignment,
    BulkImportResult,
    Lineup,
    RotationState,
    SubmissionLog,
    WildcardEntry,
)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", text.strip()).strip("_")
    return slug or "account"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Repository:
    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        cipher: Optional[CredentialCipher] = None,
    ) -> None:
        self.conn = conn or database.connect()
        self.cipher = cipher or CredentialCipher()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    def _unique_profile_dir(self, email: str) -> str:
        base = _slugify(email)
        candidate = config.CHROME_PROFILES_DIR / base
        i = 1
        while candidate.exists() or self._profile_dir_taken(str(candidate)):
            candidate = config.CHROME_PROFILES_DIR / f"{base}_{i}"
            i += 1
        return str(candidate)

    def _profile_dir_taken(self, profile_dir: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM accounts WHERE profile_dir = ?", (profile_dir,)
        ).fetchone()
        return row is not None

    def add_account(self, label: str, email: str, password: str) -> Account:
        profile_dir = self._unique_profile_dir(email)
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        created = _now()
        cur = self.conn.execute(
            """INSERT INTO accounts
                 (label, email, password_enc, profile_dir, session_valid, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (label, email, self.cipher.encrypt(password), profile_dir, created),
        )
        self.conn.commit()
        return Account(
            id=cur.lastrowid,
            label=label,
            email=email,
            password=password,
            profile_dir=profile_dir,
            session_valid=False,
            created_at=created,
        )

    def _row_to_account(self, row: sqlite3.Row, include_password: bool) -> Account:
        return Account(
            id=row["id"],
            label=row["label"],
            email=row["email"],
            password=self.cipher.decrypt(row["password_enc"]) if include_password else "",
            profile_dir=row["profile_dir"],
            session_valid=bool(row["session_valid"]),
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    def list_accounts(self, include_password: bool = False) -> list[Account]:
        # Logged-in (session-valid) accounts first, then not-logged-in ones.
        # Valid accounts are ordered by WHEN they logged in (last_login_at ASC),
        # so a freshly logged-in account drops to the BOTTOM of the valid group
        # instead of jumping into the middle by creation order. Not-logged-in
        # accounts (NULL last_login_at) keep their add order (id ASC) at the end.
        rows = self.conn.execute(
            "SELECT * FROM accounts "
            "ORDER BY session_valid DESC, last_login_at ASC, id ASC"
        ).fetchall()
        return [self._row_to_account(r, include_password) for r in rows]

    def get_account(self, account_id: int, include_password: bool = True) -> Optional[Account]:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return self._row_to_account(row, include_password) if row else None

    def update_account(
        self,
        account_id: int,
        label: str,
        email: str,
        password: Optional[str] = None,
    ) -> None:
        """Update account. If ``password`` is None, the existing one is kept."""
        if password is None:
            self.conn.execute(
                "UPDATE accounts SET label = ?, email = ? WHERE id = ?",
                (label, email, account_id),
            )
        else:
            self.conn.execute(
                "UPDATE accounts SET label = ?, email = ?, password_enc = ? WHERE id = ?",
                (label, email, self.cipher.encrypt(password), account_id),
            )
        self.conn.commit()

    def set_session_valid(self, account_id: int, valid: bool) -> None:
        self.conn.execute(
            "UPDATE accounts SET session_valid = ?, last_login_at = ? WHERE id = ?",
            (1 if valid else 0, _now() if valid else None, account_id),
        )
        self.conn.commit()

    def delete_account(self, account_id: int) -> None:
        self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.conn.commit()

    def count_accounts(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]

    def email_exists(self, email: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM accounts WHERE email = ? COLLATE NOCASE", (email.strip(),)
        ).fetchone()
        return row is not None

    def clear_all_accounts(self) -> int:
        """Delete every account (and cascaded assignments/rotation state).

        Chrome profile folders on disk are left in place so re-importing the
        same email keeps its saved session.
        """
        n = self.count_accounts()
        self.conn.execute("DELETE FROM accounts")
        self.conn.commit()
        return n

    def bulk_import_accounts(self, text: str) -> "BulkImportResult":
        """Import ``email:password`` lines (one per line).

        Blank lines and lines starting with '#' are ignored. The password may
        itself contain ':' (only the first ':' splits). Duplicate emails
        (already stored, or repeated within the paste) are skipped and
        reported. A label is auto-derived from the email local-part; you can
        rename it later on the Accounts screen.
        """
        result = BulkImportResult()
        existing = {a.email.casefold() for a in self.list_accounts()}
        seen_in_batch: set[str] = set()

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Accept both ':' and (as a courtesy) whitespace/tab separation.
            if ":" in line:
                email, password = line.split(":", 1)
            elif "\t" in line:
                email, password = line.split("\t", 1)
            else:
                result.errors.append(f"Malformed (no ':'): {line[:40]}")
                continue
            email = email.strip()
            password = password.strip()
            if not email or not password:
                result.errors.append(f"Missing email or password: {line[:40]}")
                continue
            key = email.casefold()
            if key in existing:
                result.skipped_existing += 1
                continue
            if key in seen_in_batch:
                result.skipped_duplicate += 1
                continue
            seen_in_batch.add(key)
            label = email.split("@", 1)[0]
            try:
                self.add_account(label, email, password)
                result.imported += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{email}: {exc}")
        return result

    # ------------------------------------------------------------------ #
    # Roster cache (scraped rider names)
    # ------------------------------------------------------------------ #
    def set_roster(self, riders: list[str]) -> None:
        self.conn.execute("DELETE FROM roster")
        for pos, rider in enumerate(riders):
            rider = rider.strip()
            if rider:
                self.conn.execute(
                    "INSERT OR IGNORE INTO roster (rider, position) VALUES (?, ?)",
                    (rider, pos),
                )
        self.conn.commit()
        self.set_meta("roster_updated_at", _now())

    def get_roster(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT rider FROM roster ORDER BY position, id"
        ).fetchall()
        return [r["rider"] for r in rows]

    def roster_updated_at(self) -> Optional[str]:
        return self.get_meta("roster_updated_at")

    # ------------------------------------------------------------------ #
    # Meta key/value
    # ------------------------------------------------------------------ #
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------ #
    # Name aliases / overrides (pin an ambiguous query to a rider)
    # ------------------------------------------------------------------ #
    def set_alias(self, query: str, rider: str) -> None:
        q = normalize_query(query)
        rider = (rider or "").strip()
        if not q or not rider:
            return
        self.conn.execute(
            "INSERT INTO name_aliases (query, rider) VALUES (?, ?) "
            "ON CONFLICT(query) DO UPDATE SET rider = excluded.rider",
            (q, rider),
        )
        self.conn.commit()

    def delete_alias(self, query: str) -> None:
        self.conn.execute("DELETE FROM name_aliases WHERE query = ?", (normalize_query(query),))
        self.conn.commit()

    def get_aliases(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT query, rider FROM name_aliases").fetchall()
        return {r["query"]: r["rider"] for r in rows}

    # ------------------------------------------------------------------ #
    # Round lifecycle
    # ------------------------------------------------------------------ #
    def reset_round(self) -> None:
        """Clear the current round for a fresh week.

        Removes: round lineup/wildcard text, the locked plan, per-account run
        statuses, and the submission history.
        Preserves: accounts (always) and saved name overrides (aliases).
        """
        for key in (
            "round_lineups_text", "round_wildcards_text",
            "round_plan_json", "round_status_json", "round_start_account_id",
        ):
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self.conn.execute("DELETE FROM submission_log")
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Lineups
    # ------------------------------------------------------------------ #
    def add_lineup(self, name: str, riders: list[str]) -> Lineup:
        cur = self.conn.execute(
            "INSERT INTO lineups (name, riders) VALUES (?, ?)",
            (name, json.dumps(riders)),
        )
        self.conn.commit()
        return Lineup(id=cur.lastrowid, name=name, riders=list(riders))

    def update_lineup(self, lineup_id: int, name: str, riders: list[str]) -> None:
        self.conn.execute(
            "UPDATE lineups SET name = ?, riders = ? WHERE id = ?",
            (name, json.dumps(riders), lineup_id),
        )
        self.conn.commit()

    def list_lineups(self) -> list[Lineup]:
        rows = self.conn.execute(
            "SELECT * FROM lineups ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [
            Lineup(id=r["id"], name=r["name"], riders=json.loads(r["riders"]))
            for r in rows
        ]

    def get_lineup(self, lineup_id: int) -> Optional[Lineup]:
        row = self.conn.execute(
            "SELECT * FROM lineups WHERE id = ?", (lineup_id,)
        ).fetchone()
        if not row:
            return None
        return Lineup(id=row["id"], name=row["name"], riders=json.loads(row["riders"]))

    def delete_lineup(self, lineup_id: int) -> None:
        self.conn.execute("DELETE FROM lineups WHERE id = ?", (lineup_id,))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Wildcard pool
    # ------------------------------------------------------------------ #
    def set_wildcard_pool(self, riders: list[str]) -> None:
        """Replace the entire ordered wildcard pool.

        Existing rotation positions remain valid because we treat the pool as
        an ordered list; rotation logic clamps/moduloes against the new size.
        """
        self.conn.execute("DELETE FROM wildcard_pool")
        for pos, rider in enumerate(riders):
            rider = rider.strip()
            if rider:
                self.conn.execute(
                    "INSERT OR IGNORE INTO wildcard_pool (rider, position) VALUES (?, ?)",
                    (rider, pos),
                )
        self.conn.commit()

    def list_wildcards(self) -> list[WildcardEntry]:
        rows = self.conn.execute(
            "SELECT * FROM wildcard_pool ORDER BY position, id"
        ).fetchall()
        return [
            WildcardEntry(id=r["id"], rider=r["rider"], position=r["position"])
            for r in rows
        ]

    def wildcard_riders(self) -> list[str]:
        return [w.rider for w in self.list_wildcards()]

    # ------------------------------------------------------------------ #
    # Assignments
    # ------------------------------------------------------------------ #
    def set_assignment(self, account_id: int, lineup_id: int, enabled: bool) -> None:
        if enabled:
            self.conn.execute(
                """INSERT INTO assignments (account_id, lineup_id, enabled)
                   VALUES (?, ?, 1)
                   ON CONFLICT(account_id, lineup_id) DO UPDATE SET enabled = 1""",
                (account_id, lineup_id),
            )
        else:
            self.conn.execute(
                "DELETE FROM assignments WHERE account_id = ? AND lineup_id = ?",
                (account_id, lineup_id),
            )
        self.conn.commit()

    def list_assignments(self) -> list[Assignment]:
        rows = self.conn.execute("SELECT * FROM assignments").fetchall()
        return [
            Assignment(
                id=r["id"],
                account_id=r["account_id"],
                lineup_id=r["lineup_id"],
                enabled=bool(r["enabled"]),
            )
            for r in rows
        ]

    def assignments_for_account(self, account_id: int) -> list[Assignment]:
        rows = self.conn.execute(
            "SELECT * FROM assignments WHERE account_id = ? AND enabled = 1",
            (account_id,),
        ).fetchall()
        return [
            Assignment(
                id=r["id"],
                account_id=r["account_id"],
                lineup_id=r["lineup_id"],
                enabled=bool(r["enabled"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Rotation state
    # ------------------------------------------------------------------ #
    def get_rotation_state(self, account_id: int, lineup_id: int) -> RotationState:
        row = self.conn.execute(
            "SELECT * FROM rotation_state WHERE account_id = ? AND lineup_id = ?",
            (account_id, lineup_id),
        ).fetchone()
        if row:
            return RotationState(
                account_id=row["account_id"],
                lineup_id=row["lineup_id"],
                last_wildcard_position=row["last_wildcard_position"],
                round_number=row["round_number"],
                updated_at=row["updated_at"],
            )
        return RotationState(account_id=account_id, lineup_id=lineup_id)

    def save_rotation_state(self, state: RotationState) -> None:
        self.conn.execute(
            """INSERT INTO rotation_state
                 (account_id, lineup_id, last_wildcard_position, round_number, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id, lineup_id) DO UPDATE SET
                 last_wildcard_position = excluded.last_wildcard_position,
                 round_number = excluded.round_number,
                 updated_at = excluded.updated_at""",
            (
                state.account_id,
                state.lineup_id,
                state.last_wildcard_position,
                state.round_number,
                _now(),
            ),
        )
        self.conn.commit()

    def reset_rotation(self, account_id: int, lineup_id: int) -> None:
        self.conn.execute(
            "DELETE FROM rotation_state WHERE account_id = ? AND lineup_id = ?",
            (account_id, lineup_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Submission log
    # ------------------------------------------------------------------ #
    def add_submission_log(self, entry: SubmissionLog) -> int:
        cur = self.conn.execute(
            """INSERT INTO submission_log
                 (account_label, account_email, lineup_name, core_five, wildcard,
                  round_number, round_label, success, message, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.account_label,
                entry.account_email,
                entry.lineup_name,
                entry.core_five,
                entry.wildcard,
                entry.round_number,
                entry.round_label,
                1 if entry.success else 0,
                entry.message,
                entry.timestamp,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_submission_logs(self, limit: int = 500) -> list[SubmissionLog]:
        rows = self.conn.execute(
            "SELECT * FROM submission_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            SubmissionLog(
                id=r["id"],
                account_label=r["account_label"],
                account_email=r["account_email"],
                lineup_name=r["lineup_name"],
                core_five=r["core_five"],
                wildcard=r["wildcard"],
                round_number=r["round_number"],
                round_label=r["round_label"],
                success=bool(r["success"]),
                message=r["message"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]
