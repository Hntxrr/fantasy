"""Tests for fuzzy rider-name resolution."""

from __future__ import annotations

import pytest

from rmfantasy.resolver import RiderResolver

ROSTER = [
    "Aaron Plessinger", "Antonio Cairoli", "Benny Bloss", "Christian Craig",
    "Colt Nichols", "Cooper Webb", "Cornelius Tondel", "Dylan Ferrandis",
    "Eli Tomac", "Fredrik Noren", "Garrett Marchbanks", "Grant Harlan",
    "Haiden Deegan", "Hunter Lawrence", "Jett Lawrence", "Jordon Smith",
    "Jorge Prado", "Justin Barcia", "Justin Cooper", "Lorenzo Locurcio",
    "Lucas Coenen", "Malcolm Stewart", "Mikkel Haarup", "Mitchell Harrison",
    "RJ Hampshire", "Valentin Guillod",
    # duplicated on purpose (dropdowns list riders twice: featured + full)
    "Jett Lawrence", "Haiden Deegan",
]


@pytest.fixture(scope="module")
def resolver():
    return RiderResolver(ROSTER)


def test_dedup(resolver):
    # 26 unique names despite the 2 duplicates in the input.
    assert len(resolver.roster) == 26


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Jett", "Jett Lawrence"),
        ("Hunter", "Hunter Lawrence"),
        ("Haiden", "Haiden Deegan"),
        ("Jorge", "Jorge Prado"),
        ("Eli", "Eli Tomac"),
        ("Mikkel", "Mikkel Haarup"),
        ("Cornelius", "Cornelius Tondel"),
        ("Valentine", "Valentin Guillod"),      # partial / off-by-one
        ("Jordan smith", "Jordon Smith"),        # typo tolerated
        ("Justin barcia", "Justin Barcia"),      # disambiguated by last name
        ("Mitchell harrison", "Mitchell Harrison"),
        ("Antonio cairoli", "Antonio Cairoli"),
        ("Lorenzo Loc", "Lorenzo Locurcio"),     # partial last name
        ("Aaron", "Aaron Plessinger"),
    ],
)
def test_resolves(resolver, query, expected):
    res = resolver.resolve(query)
    assert res.name == expected, f"{query!r} -> {res.name!r} (score {res.score})"
    assert res.score >= 0.6


def test_ambiguous_first_name_flagged(resolver):
    # "Justin" matches both Justin Barcia and Justin Cooper.
    res = resolver.resolve("Justin")
    assert res.ambiguous is True
    assert not res.ok  # ambiguous results are not auto-accepted


def test_exact_full_name_not_ambiguous(resolver):
    res = resolver.resolve("Justin Barcia")
    assert res.name == "Justin Barcia"
    assert res.ambiguous is False
    assert res.ok


def test_unresolved_returns_none(resolver):
    res = resolver.resolve("Zxqwerty Notarider")
    assert res.name is None
    assert not res.ok


def test_resolve_lineup_line(resolver):
    results = resolver.resolve_lineup_line("Jett Hunter Haiden Eli Jorge")
    names = [r.name for r in results]
    assert names == [
        "Jett Lawrence", "Hunter Lawrence", "Haiden Deegan",
        "Eli Tomac", "Jorge Prado",
    ]


def test_empty_roster():
    r = RiderResolver([])
    assert r.resolve("Jett").name is None
