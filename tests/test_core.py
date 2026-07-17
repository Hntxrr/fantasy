"""Tests for the non-UI core: crypto, repository CRUD, and rotation logic.

Run with:  pytest -q   (RMFANTASY_HOME is pointed at a temp dir per test)
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Fresh app home + repository backed by a temp SQLite file and file key."""
    monkeypatch.setenv("RMFANTASY_HOME", str(tmp_path))
    # Force file-based key (no OS keyring) for deterministic tests.
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None)
    monkeypatch.setattr("keyring.set_password", lambda *a, **k: (_ for _ in ()).throw(Exception("no keyring")))

    # Reload modules so they pick up the patched RMFANTASY_HOME.
    from rmfantasy import config as config_mod
    importlib.reload(config_mod)
    from rmfantasy import crypto as crypto_mod
    importlib.reload(crypto_mod)
    from rmfantasy import database as db_mod
    importlib.reload(db_mod)
    from rmfantasy import repository as repo_mod
    importlib.reload(repo_mod)

    r = repo_mod.Repository()
    yield r
    r.close()


# --------------------------------------------------------------------------- #
# Crypto
# --------------------------------------------------------------------------- #
def test_crypto_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RMFANTASY_HOME", str(tmp_path))
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None)
    monkeypatch.setattr("keyring.set_password", lambda *a, **k: (_ for _ in ()).throw(Exception("no keyring")))
    from rmfantasy import config as config_mod
    importlib.reload(config_mod)
    from rmfantasy import crypto as crypto_mod
    importlib.reload(crypto_mod)

    cipher = crypto_mod.CredentialCipher()
    token = cipher.encrypt("hunter2!@#")
    assert token != b"hunter2!@#"
    assert b"hunter2" not in token  # not stored in plaintext
    assert cipher.decrypt(token) == "hunter2!@#"

    # A second cipher using the persisted file key decrypts the same token.
    cipher2 = crypto_mod.CredentialCipher()
    assert cipher2.decrypt(token) == "hunter2!@#"


def test_key_file_created(tmp_path, monkeypatch):
    monkeypatch.setenv("RMFANTASY_HOME", str(tmp_path))
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None)
    monkeypatch.setattr("keyring.set_password", lambda *a, **k: (_ for _ in ()).throw(Exception("no keyring")))
    from rmfantasy import config as config_mod
    importlib.reload(config_mod)
    from rmfantasy import crypto as crypto_mod
    importlib.reload(crypto_mod)

    crypto_mod.get_or_create_key()
    assert config_mod.KEY_PATH.exists()


# --------------------------------------------------------------------------- #
# Repository: accounts
# --------------------------------------------------------------------------- #
def test_account_crud_and_encryption(repo):
    acc = repo.add_account("Main", "me@example.com", "s3cret")
    assert acc.id is not None
    assert acc.profile_dir  # a profile dir was assigned

    # Password stored encrypted: raw DB blob must not contain plaintext.
    row = repo.conn.execute(
        "SELECT password_enc FROM accounts WHERE id = ?", (acc.id,)
    ).fetchone()
    assert b"s3cret" not in row["password_enc"]

    loaded = repo.get_account(acc.id, include_password=True)
    assert loaded.password == "s3cret"

    repo.update_account(acc.id, "Main2", "me2@example.com", password="newpw")
    loaded = repo.get_account(acc.id, include_password=True)
    assert loaded.label == "Main2"
    assert loaded.email == "me2@example.com"
    assert loaded.password == "newpw"

    # Update without changing password keeps it.
    repo.update_account(acc.id, "Main3", "me2@example.com", password=None)
    loaded = repo.get_account(acc.id, include_password=True)
    assert loaded.password == "newpw"

    repo.delete_account(acc.id)
    assert repo.get_account(acc.id) is None


def test_unique_profile_dirs(repo):
    a1 = repo.add_account("A", "same@example.com", "p")
    # Different email -> different slug/profile dir.
    a2 = repo.add_account("B", "other@example.com", "p")
    assert a1.profile_dir != a2.profile_dir


# --------------------------------------------------------------------------- #
# Repository: lineups & wildcards
# --------------------------------------------------------------------------- #
def test_lineup_crud(repo):
    lu = repo.add_lineup("Lineup A", ["Jett Lawrence", "Hunter Lawrence", "Chase Sexton", "Eli Tomac", "Cooper Webb"])
    assert lu.is_complete()
    fetched = repo.get_lineup(lu.id)
    assert fetched.riders[0] == "Jett Lawrence"
    repo.update_lineup(lu.id, "Lineup A", ["A", "B", "C", "D", "E"])
    assert repo.get_lineup(lu.id).riders == ["A", "B", "C", "D", "E"]
    repo.delete_lineup(lu.id)
    assert repo.get_lineup(lu.id) is None


def test_wildcard_pool(repo):
    repo.set_wildcard_pool(["Deegan", "Prado", "Coenen", "", "Deegan"])  # dup + blank
    riders = repo.wildcard_riders()
    assert riders == ["Deegan", "Prado", "Coenen"]  # blanks dropped, dup ignored


# --------------------------------------------------------------------------- #
# Repository: assignments
# --------------------------------------------------------------------------- #
def test_assignments(repo):
    acc = repo.add_account("A", "a@example.com", "p")
    lu = repo.add_lineup("L", ["1", "2", "3", "4", "5"])
    repo.set_assignment(acc.id, lu.id, True)
    assert len(repo.assignments_for_account(acc.id)) == 1
    repo.set_assignment(acc.id, lu.id, False)
    assert repo.assignments_for_account(acc.id) == []


# --------------------------------------------------------------------------- #
# Rotation logic
# --------------------------------------------------------------------------- #
def test_rotation_cycles_before_repeat(repo):
    from rmfantasy import rotation
    from rmfantasy.models import RotationState

    pool = ["W1", "W2", "W3"]
    state = RotationState(account_id=1, lineup_id=1)
    seen = []
    for _ in range(len(pool)):
        state, rider = rotation.advance(state, pool)
        seen.append(rider)
    # Full pool used exactly once before any repeat.
    assert sorted(seen) == sorted(pool)
    # Next one wraps back to the first.
    state, rider = rotation.advance(state, pool)
    assert rider == "W1"


def test_rotation_persistence(repo):
    from rmfantasy import rotation

    acc = repo.add_account("A", "a@example.com", "p")
    lu = repo.add_lineup("L", ["1", "2", "3", "4", "5"])
    pool = ["W1", "W2", "W3"]

    state = repo.get_rotation_state(acc.id, lu.id)
    new_state, rider = rotation.advance(state, pool)
    assert rider == "W1"
    repo.save_rotation_state(new_state)

    # Reload from DB simulates an app restart.
    reloaded = repo.get_rotation_state(acc.id, lu.id)
    assert reloaded.last_wildcard_position == 0
    assert reloaded.round_number == 1

    next_state, rider2 = rotation.advance(reloaded, pool)
    assert rider2 == "W2"
    assert next_state.round_number == 2


def test_rotation_empty_pool_raises():
    from rmfantasy import rotation
    from rmfantasy.models import RotationState

    with pytest.raises(rotation.RotationError):
        rotation.advance(RotationState(account_id=1, lineup_id=1), [])


def test_full_cycle_order():
    from rmfantasy import rotation
    order = rotation.full_cycle_order(["A", "B", "C"], start_after=0)
    assert order == ["B", "C", "A"]


# --------------------------------------------------------------------------- #
# Submission log
# --------------------------------------------------------------------------- #
def test_submission_log(repo):
    from rmfantasy.models import SubmissionLog
    entry = SubmissionLog(
        id=None, account_label="A", account_email="a@example.com",
        lineup_name="L", core_five="1,2,3,4,5", wildcard="W1",
        round_number=1, round_label="Round #7", success=True, message="ok",
    )
    repo.add_submission_log(entry)
    logs = repo.list_submission_logs()
    assert len(logs) == 1
    assert logs[0].wildcard == "W1"
    assert logs[0].success is True



# --------------------------------------------------------------------------- #
# Bulk import / clear-all
# --------------------------------------------------------------------------- #
def test_bulk_import(repo):
    text = """
    # a comment line
    alice@example.com:pw1
    bob@example.com:pw:with:colons
    alice@example.com:dupe        
    malformed-line-no-colon
    carol@example.com:
    """
    result = repo.bulk_import_accounts(text)
    assert result.imported == 2                 # alice, bob
    assert result.skipped_duplicate == 1        # second alice
    assert len(result.errors) == 2              # malformed + carol(empty pw)

    accounts = repo.list_accounts(include_password=True)
    emails = {a.email for a in accounts}
    assert emails == {"alice@example.com", "bob@example.com"}
    bob = next(a for a in accounts if a.email == "bob@example.com")
    assert bob.password == "pw:with:colons"     # only first ':' splits

    # Re-import skips existing.
    result2 = repo.bulk_import_accounts("alice@example.com:x\ndan@example.com:pw")
    assert result2.imported == 1
    assert result2.skipped_existing == 1


def test_clear_all_accounts(repo):
    repo.bulk_import_accounts("a@x.com:1\nb@x.com:2\nc@x.com:3")
    assert repo.count_accounts() == 3
    removed = repo.clear_all_accounts()
    assert removed == 3
    assert repo.count_accounts() == 0


# --------------------------------------------------------------------------- #
# Roster + meta
# --------------------------------------------------------------------------- #
def test_roster_and_meta(repo):
    repo.set_roster(["Jett Lawrence", "Hunter Lawrence", "Jett Lawrence"])  # dup
    assert repo.get_roster() == ["Jett Lawrence", "Hunter Lawrence"]
    assert repo.roster_updated_at() is not None

    repo.set_meta("round_lineups_text", "Jett Hunter Haiden Eli Jorge")
    assert repo.get_meta("round_lineups_text") == "Jett Hunter Haiden Eli Jorge"
    assert repo.get_meta("missing", "default") == "default"
