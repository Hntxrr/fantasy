"""Fuzzy rider-name resolution.

You type partial names, first names, or slight misspellings; this module maps
them to the exact roster names scraped from the site's dropdown, e.g.:

    "Jett"           -> "Jett Lawrence"
    "Hunter"         -> "Hunter Lawrence"
    "Jordan smith"   -> "Jordon Smith"      (typo tolerated)
    "Valentine"      -> "Valentin Guillod"  (partial / off-by-one)
    "Cornelius"      -> "Cornelius Tondel"
    "Mikkel"         -> "Mikkel Haarup"

The resolver is pure (no DB, no Selenium) and returns a confidence score plus
alternatives so the UI can show a preview and flag anything ambiguous or
unresolved for you to confirm before running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Accept a match at/above this score; below it we treat the query as unresolved.
ACCEPT_THRESHOLD = 0.60
# If the top two candidates are this close AND both strong, flag as ambiguous.
AMBIGUOUS_GAP = 0.06
AMBIGUOUS_MIN_SCORE = 0.72


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().casefold()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_similarity(q: str, r: str) -> float:
    """Similarity between two single name tokens (0..1)."""
    if not q or not r:
        return 0.0
    if q == r:
        return 1.0
    # Prefix relationships score high (partial names like "Jett", "Mikkel").
    if r.startswith(q):
        # longer shared prefix relative to the roster token = better
        return 0.90 + 0.09 * (len(q) / len(r))
    if q.startswith(r):
        return 0.86 + 0.09 * (len(r) / len(q))
    # Otherwise fall back to edit-distance style ratio (typos: jordan/jordon).
    return _ratio(q, r)


def _name_score(query: str, roster_name: str) -> float:
    """Score how well ``query`` matches a full ``roster_name`` (0..1)."""
    q = _norm(query)
    r = _norm(roster_name)
    if not q:
        return 0.0
    if q == r:
        return 1.0

    q_tokens = q.split()
    r_tokens = r.split()

    # Whole-string fuzzy ratio (handles multi-word queries + typos).
    whole = _ratio(q, r)

    if len(q_tokens) == 1:
        # Single token: match against the best roster token, but weight the
        # first name (index 0) most since people usually type the first name.
        best_any = max((_token_similarity(q_tokens[0], rt) for rt in r_tokens), default=0.0)
        first = _token_similarity(q_tokens[0], r_tokens[0]) if r_tokens else 0.0
        token_score = max(best_any, first * 1.0)
        # A strong first-name prefix should dominate a weak whole-string ratio.
        return max(token_score, whole)

    # Multi-token query: align each query token to its best roster token.
    per_token = []
    for qt in q_tokens:
        per_token.append(max((_token_similarity(qt, rt) for rt in r_tokens), default=0.0))
    aligned = sum(per_token) / len(per_token)
    # Blend token alignment with whole-string ratio; alignment weighted higher.
    return max(whole, 0.7 * aligned + 0.3 * whole)


@dataclass
class ResolveResult:
    query: str
    name: str | None                       # best match, or None if unresolved
    score: float = 0.0
    ambiguous: bool = False
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.name is not None and not self.ambiguous


class RiderResolver:
    def __init__(self, roster: list[str]):
        # Dedupe while preserving order (dropdowns list each rider twice).
        seen = set()
        self.roster: list[str] = []
        for name in roster:
            name = " ".join((name or "").split()).strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                self.roster.append(name)

    def resolve(self, query: str, top_k: int = 4) -> ResolveResult:
        query = (query or "").strip()
        if not query:
            return ResolveResult(query=query, name=None, score=0.0)
        if not self.roster:
            return ResolveResult(query=query, name=None, score=0.0)

        scored = sorted(
            ((name, _name_score(query, name)) for name in self.roster),
            key=lambda t: t[1],
            reverse=True,
        )
        best_name, best_score = scored[0]
        alternatives = scored[1:top_k]

        if best_score < ACCEPT_THRESHOLD:
            return ResolveResult(
                query=query, name=None, score=best_score, alternatives=alternatives
            )

        ambiguous = False
        # An EXACT full-name match is always definitive. Otherwise, if the top
        # two candidates are both strong and nearly tied (e.g. two riders share
        # a first name), flag it so the operator can disambiguate.
        exact_full = _norm(query) == _norm(best_name)
        if not exact_full and len(scored) > 1:
            second_score = scored[1][1]
            if (
                best_score - second_score < AMBIGUOUS_GAP
                and second_score >= AMBIGUOUS_MIN_SCORE
            ):
                ambiguous = True

        return ResolveResult(
            query=query,
            name=best_name,
            score=round(best_score, 3),
            ambiguous=ambiguous,
            alternatives=[(n, round(s, 3)) for n, s in alternatives],
        )

    def resolve_many(self, queries: list[str]) -> list[ResolveResult]:
        return [self.resolve(q) for q in queries]

    def resolve_lineup_line(self, line: str) -> list[ResolveResult]:
        """A lineup line is 5 space-separated single-name tokens (1st..5th)."""
        tokens = line.split()
        return [self.resolve(tok) for tok in tokens]
