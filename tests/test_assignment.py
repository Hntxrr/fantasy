"""Tests for cartesian lineup x wildcard -> account assignment."""

from __future__ import annotations

from rmfantasy.assignment import build_plan
from rmfantasy.models import Account


def _accounts(n: int) -> list[Account]:
    return [
        Account(id=i + 1, label=f"acct{i + 1}", email=f"a{i + 1}@x.com",
                profile_dir=f"/p/{i + 1}")
        for i in range(n)
    ]


def _lineups(n: int) -> list[list[str]]:
    return [[f"L{i}-1", f"L{i}-2", f"L{i}-3", f"L{i}-4", f"L{i}-5"] for i in range(n)]


def test_20x8_maps_to_160_accounts():
    lineups = _lineups(20)
    wildcards = [f"W{i}" for i in range(8)]
    plan = build_plan(lineups, wildcards, _accounts(160))
    assert plan.pairs_needed == 160
    assert plan.assigned_count == 160
    assert plan.balanced

    # Accounts 1-8 -> lineup 1 with wildcards W0..W7 (sequential).
    first8 = plan.assignments[:8]
    assert all(a.lineup_index == 1 for a in first8)
    assert [a.wildcard for a in first8] == wildcards
    # Accounts 9-16 -> lineup 2.
    assert all(a.lineup_index == 2 for a in plan.assignments[8:16])


def test_fewer_accounts_reports_unassigned_pairs():
    plan = build_plan(_lineups(2), ["W0", "W1", "W2"], _accounts(4))  # need 6, have 4
    assert plan.pairs_needed == 6
    assert plan.assigned_count == 4
    assert len(plan.unassigned_pairs) == 2
    assert not plan.idle_accounts
    assert not plan.balanced


def test_more_accounts_reports_idle():
    plan = build_plan(_lineups(1), ["W0", "W1"], _accounts(5))  # need 2, have 5
    assert plan.assigned_count == 2
    assert len(plan.idle_accounts) == 3
    assert not plan.unassigned_pairs


def test_pair_ordering_is_lineup_major():
    plan = build_plan(_lineups(2), ["W0", "W1"], _accounts(4))
    seq = [(a.lineup_index, a.wildcard) for a in plan.assignments]
    assert seq == [(1, "W0"), (1, "W1"), (2, "W0"), (2, "W1")]
