"""Wildcard rotation logic.

Rules implemented:
  * Each round pairs a core-five lineup with exactly ONE wildcard rider.
  * For a given (account, lineup) pair, the full wildcard pool is cycled
    through before any wildcard repeats.
  * Rotation state (last position used + round number) is persisted by the
    repository so rotation survives app restarts.

This module is deliberately pure (no DB, no Selenium) so it is easy to test.
The caller:
  1. calls ``peek_next()`` to know which wildcard WOULD be used,
  2. performs the submission,
  3. on success, calls ``advance()`` to get the new RotationState and persists
     it. State is only advanced after a confirmed successful submission, so a
     failed round does not "burn" a wildcard.
"""

from __future__ import annotations

from dataclasses import replace

from .models import RotationState


class RotationError(Exception):
    """Raised when rotation cannot be computed (e.g. empty pool)."""


def _next_position(last_position: int, pool_size: int) -> int:
    """Return the next index in a cyclic pool.

    A last_position of -1 (nothing used yet) yields 0. Otherwise we advance by
    one and wrap. If the pool shrank below last_position, the modulo keeps us
    in range and continues the cycle sensibly.
    """
    if pool_size <= 0:
        raise RotationError("The wildcard pool is empty. Add wildcard riders first.")
    if last_position < 0:
        return 0
    return (last_position + 1) % pool_size


def peek_next(state: RotationState, pool: list[str]) -> tuple[int, str]:
    """Return (position, rider) for the wildcard that would be used next.

    Does not mutate state.
    """
    if not pool:
        raise RotationError("The wildcard pool is empty. Add wildcard riders first.")
    pos = _next_position(state.last_wildcard_position, len(pool))
    return pos, pool[pos]


def advance(state: RotationState, pool: list[str]) -> tuple[RotationState, str]:
    """Compute the next wildcard and return (new_state, rider).

    The returned state has the new position and an incremented round number.
    Persist it only after a successful submission.
    """
    pos, rider = peek_next(state, pool)
    new_state = replace(
        state,
        last_wildcard_position=pos,
        round_number=state.round_number + 1,
    )
    return new_state, rider


def full_cycle_order(pool: list[str], start_after: int = -1) -> list[str]:
    """Return the order wildcards will be used across a full cycle.

    Useful for previewing the rotation in the UI.
    """
    if not pool:
        return []
    n = len(pool)
    start = _next_position(start_after, n)
    return [pool[(start + i) % n] for i in range(n)]
