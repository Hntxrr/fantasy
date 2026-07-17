"""Plain data models (dataclasses) used across the app."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Account:
    """A stored RMFantasySMX account.

    ``password`` here is the *plaintext* password only when set in memory
    right before saving; on load from the DB it is decrypted on demand via the
    repository. The repository stores an encrypted blob, never plaintext.
    """

    id: Optional[int]
    label: str                 # friendly name shown in the UI
    email: str                 # login email/username
    password: str = ""         # plaintext (transient) -- encrypted at rest
    profile_dir: str = ""      # isolated Chrome profile directory
    session_valid: bool = False  # last known login state
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


@dataclass
class Lineup:
    """A named 'core five' lineup: exactly five riders for places 1-5."""

    id: Optional[int]
    name: str
    riders: list[str] = field(default_factory=list)  # length 5, ordered 1st..5th

    def is_complete(self) -> bool:
        return len([r for r in self.riders if r.strip()]) == 5


@dataclass
class WildcardEntry:
    """One rider in the wildcard pool, with a stable ordering position."""

    id: Optional[int]
    rider: str
    position: int = 0


@dataclass
class Assignment:
    """Maps a lineup to an account (many lineups may run on one account)."""

    id: Optional[int]
    account_id: int
    lineup_id: int
    enabled: bool = True


@dataclass
class RotationState:
    """Tracks which wildcard was last used for a given (account, lineup).

    ``last_wildcard_position`` is the index into the ordered wildcard pool that
    was used most recently. -1 means "nothing used yet".
    """

    account_id: int
    lineup_id: int
    last_wildcard_position: int = -1
    round_number: int = 0
    updated_at: Optional[str] = None


@dataclass
class BulkImportResult:
    """Outcome of a bulk email:password import."""

    imported: int = 0
    skipped_existing: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.imported} imported"]
        if self.skipped_existing:
            parts.append(f"{self.skipped_existing} already existed")
        if self.skipped_duplicate:
            parts.append(f"{self.skipped_duplicate} duplicate lines")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


@dataclass
class SubmissionLog:
    """One row per submission attempt."""

    id: Optional[int]
    account_label: str
    account_email: str
    lineup_name: str
    core_five: str            # comma-joined rider names
    wildcard: str
    round_number: int
    round_label: str          # e.g. "Round #7 - Spring Creek"
    success: bool
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
