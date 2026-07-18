"""Random identity generation + data models for the Sign Up flow.

The Sign Up tab lets you paste a list of emails and one shared mailing address;
for each email the bot fabricates a plausible-but-random identity (first name,
last name, US phone number, leaderboard nickname and a strong password), fills
the site's registration form, ticks "I am 18 or older", and submits.

Nothing here touches Selenium or the database -- it is pure data so it is easy
to test and reuse. ``automation.do_signup`` consumes a :class:`SignupProfile`;
``runner.SignupRunner`` orchestrates it across many emails.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Name / word pools (kept small but varied; purely cosmetic values).
# --------------------------------------------------------------------------- #
FIRST_NAMES = [
    "James", "Michael", "Robert", "John", "David", "William", "Richard",
    "Joseph", "Thomas", "Christopher", "Charles", "Daniel", "Matthew",
    "Anthony", "Mark", "Donald", "Steven", "Andrew", "Joshua", "Kevin",
    "Brian", "George", "Timothy", "Ronald", "Jason", "Ryan", "Jacob",
    "Gary", "Nicholas", "Eric", "Jonathan", "Stephen", "Justin", "Scott",
    "Brandon", "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick",
    "Jack", "Dylan", "Tyler", "Aaron", "Jose", "Adam", "Nathan", "Henry",
    "Mary", "Jennifer", "Linda", "Patricia", "Elizabeth", "Susan", "Jessica",
    "Sarah", "Karen", "Emily", "Ashley", "Amanda", "Megan", "Hannah",
    "Rachel", "Olivia", "Emma", "Madison", "Abigail", "Chloe", "Grace",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Reed", "Cook", "Morgan", "Bell", "Murphy",
]

NICKNAME_WORDS = [
    "moto", "racer", "holeshot", "berm", "whip", "scrub", "throttle",
    "braaap", "privateer", "factory", "supercross", "outdoors", "roost",
    "gate", "podium", "checkers", "wildcard", "clutch", "apex", "rhythm",
]


@dataclass
class SignupIdentity:
    """The random personal fields fabricated for one signup."""

    first_name: str
    last_name: str
    phone: str
    nickname: str
    password: str


@dataclass
class SignupProfile:
    """Everything needed to fill the registration form for one email.

    The address fields are shared across a batch; the identity fields are
    generated per email. ``country`` defaults to the United States.
    """

    email: str
    identity: SignupIdentity
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "United States"

    # Convenience passthroughs so callers can read flat attributes.
    @property
    def first_name(self) -> str:
        return self.identity.first_name

    @property
    def last_name(self) -> str:
        return self.identity.last_name

    @property
    def phone(self) -> str:
        return self.identity.phone

    @property
    def nickname(self) -> str:
        return self.identity.nickname

    @property
    def password(self) -> str:
        return self.identity.password


@dataclass
class SignupResult:
    """Outcome of one signup attempt (surfaced to the UI)."""

    email: str
    success: bool
    message: str
    password: str = ""
    account_id: Optional[int] = None


def _random_phone(rng: random.Random) -> str:
    """A random US-style phone number: (NXX) NXX-XXXX (N = 2-9)."""
    area = f"{rng.randint(2, 9)}{rng.randint(0, 9)}{rng.randint(0, 9)}"
    prefix = f"{rng.randint(2, 9)}{rng.randint(0, 9)}{rng.randint(0, 9)}"
    line = f"{rng.randint(0, 9999):04d}"
    return f"({area}) {prefix}-{line}"


def _random_password(rng: random.Random, length: int = 14) -> str:
    """A strong password with upper, lower, digit and symbol (shuffled)."""
    symbols = "!@#$%&*?"
    required = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice(symbols),
    ]
    pool = string.ascii_letters + string.digits + symbols
    rest = [rng.choice(pool) for _ in range(max(0, length - len(required)))]
    chars = required + rest
    rng.shuffle(chars)
    return "".join(chars)


def _random_nickname(rng: random.Random, first_name: str) -> str:
    """A leaderboard nickname like 'holeshot_42' or 'ryanRacer'."""
    word = rng.choice(NICKNAME_WORDS)
    style = rng.randint(0, 2)
    if style == 0:
        return f"{word}{rng.randint(1, 999)}"
    if style == 1:
        return f"{first_name.lower()}{word.capitalize()}{rng.randint(1, 99)}"
    return f"{word}_{rng.randint(10, 9999)}"


def generate_identity(rng: Optional[random.Random] = None) -> SignupIdentity:
    """Fabricate a random identity for one registration."""
    rng = rng or random.Random()
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return SignupIdentity(
        first_name=first,
        last_name=last,
        phone=_random_phone(rng),
        nickname=_random_nickname(rng, first),
        password=_random_password(rng),
    )


def build_profile(
    email: str,
    *,
    street: str = "",
    city: str = "",
    state: str = "",
    postal_code: str = "",
    country: str = "United States",
    rng: Optional[random.Random] = None,
) -> SignupProfile:
    """Create a full :class:`SignupProfile` for one email + shared address."""
    return SignupProfile(
        email=email.strip(),
        identity=generate_identity(rng),
        street=street.strip(),
        city=city.strip(),
        state=state.strip(),
        postal_code=postal_code.strip(),
        country=(country or "United States").strip() or "United States",
    )


# --------------------------------------------------------------------------- #
# US states (name + abbreviation) for the address dropdown and tolerant
# matching against whatever the site's <select> uses.
# --------------------------------------------------------------------------- #
US_STATES: list[tuple[str, str]] = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
    ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"),
    ("Delaware", "DE"), ("District of Columbia", "DC"), ("Florida", "FL"),
    ("Georgia", "GA"), ("Hawaii", "HI"), ("Idaho", "ID"), ("Illinois", "IL"),
    ("Indiana", "IN"), ("Iowa", "IA"), ("Kansas", "KS"), ("Kentucky", "KY"),
    ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
    ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"),
    ("Mississippi", "MS"), ("Missouri", "MO"), ("Montana", "MT"),
    ("Nebraska", "NE"), ("Nevada", "NV"), ("New Hampshire", "NH"),
    ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"),
    ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"),
    ("Oklahoma", "OK"), ("Oregon", "OR"), ("Pennsylvania", "PA"),
    ("Rhode Island", "RI"), ("South Carolina", "SC"), ("South Dakota", "SD"),
    ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"), ("Vermont", "VT"),
    ("Virginia", "VA"), ("Washington", "WA"), ("West Virginia", "WV"),
    ("Wisconsin", "WI"), ("Wyoming", "WY"),
]

US_STATE_NAMES: list[str] = [name for name, _ in US_STATES]


def state_variants(value: str) -> list[str]:
    """Return likely option strings for a state (name + abbreviation).

    Lets the automation match the site's <select> whether it lists full names
    ('Utah') or abbreviations ('UT'), given either as input.
    """
    v = (value or "").strip()
    if not v:
        return []
    out = [v]
    low = v.casefold()
    for name, abbr in US_STATES:
        if low in (name.casefold(), abbr.casefold()):
            out += [name, abbr]
            break
    # De-dupe, preserve order.
    seen, uniq = set(), []
    for s in out:
        if s and s.casefold() not in seen:
            seen.add(s.casefold())
            uniq.append(s)
    return uniq
