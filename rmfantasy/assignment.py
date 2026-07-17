"""Auto-assignment: pair every lineup with every wildcard, one pair per account.

Given N lineups and M wildcards, we generate N*M (lineup, wildcard) pairs in a
deterministic order and map them onto accounts sequentially:

    pairs (in order):
        Lineup 1 + Wildcard 1
        Lineup 1 + Wildcard 2
        ...
        Lineup 1 + Wildcard M      <- accounts 1..M get lineup 1
        Lineup 2 + Wildcard 1      <- accounts M+1..2M get lineup 2
        ...

Example: 20 lineups x 8 wildcards = 160 pairs -> 160 accounts.

Account order follows their stored order (import order). If the counts do not
line up exactly, we assign as many as possible and report the leftovers so the
UI can warn clearly instead of silently dropping picks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .models import Account


@dataclass
class RoundAssignment:
    account_id: int
    account_label: str
    account_email: str
    profile_dir: str
    lineup_index: int          # 1-based, for display
    core_five: list[str]       # 5 resolved rider names, ordered 1st..5th
    wildcard: str              # resolved wildcard rider name
    wildcard_index: int = 0    # 1-based, for display


@dataclass
class AssignmentPlan:
    assignments: list[RoundAssignment] = field(default_factory=list)
    lineup_count: int = 0
    wildcard_count: int = 0
    pairs_needed: int = 0
    accounts_available: int = 0
    unassigned_pairs: list[tuple[int, int]] = field(default_factory=list)  # (lineup_idx, wildcard_idx)
    idle_accounts: list[str] = field(default_factory=list)

    @property
    def assigned_count(self) -> int:
        return len(self.assignments)

    @property
    def balanced(self) -> bool:
        return not self.unassigned_pairs and not self.idle_accounts

    def summary(self) -> str:
        parts = [
            f"{self.lineup_count} lineups x {self.wildcard_count} wildcards "
            f"= {self.pairs_needed} pairs",
            f"{self.accounts_available} accounts available",
            f"{self.assigned_count} assigned",
        ]
        if self.unassigned_pairs:
            parts.append(f"{len(self.unassigned_pairs)} pairs UNASSIGNED (need more accounts)")
        if self.idle_accounts:
            parts.append(f"{len(self.idle_accounts)} accounts idle (no pair)")
        return " | ".join(parts)


def build_plan(
    lineups: Sequence[Sequence[str]],
    wildcards: Sequence[str],
    accounts: Sequence[Account],
) -> AssignmentPlan:
    """Build the account->(*lineup*, *wildcard*) plan (see module docstring)."""
    plan = AssignmentPlan(
        lineup_count=len(lineups),
        wildcard_count=len(wildcards),
        pairs_needed=len(lineups) * len(wildcards),
        accounts_available=len(accounts),
    )

    # Generate pairs in order: for each lineup, cycle all wildcards.
    pairs: list[tuple[int, int]] = []
    for li in range(len(lineups)):
        for wi in range(len(wildcards)):
            pairs.append((li, wi))

    n = min(len(pairs), len(accounts))
    for idx in range(n):
        li, wi = pairs[idx]
        account = accounts[idx]
        plan.assignments.append(
            RoundAssignment(
                account_id=account.id,
                account_label=account.label,
                account_email=account.email,
                profile_dir=account.profile_dir,
                lineup_index=li + 1,
                core_five=list(lineups[li]),
                wildcard=wildcards[wi],
                wildcard_index=wi + 1,
            )
        )

    # Leftovers.
    for idx in range(n, len(pairs)):
        li, wi = pairs[idx]
        plan.unassigned_pairs.append((li + 1, wi + 1))
    for idx in range(n, len(accounts)):
        plan.idle_accounts.append(accounts[idx].label)

    return plan
