"""Application configuration and filesystem paths.

Everything the app persists lives under a single per-user application data
directory so it is easy to back up or wipe:

    <APP_DIR>/
        rmfantasy.db            SQLite database (accounts, lineups, logs, ...)
        secret.key              Fernet encryption key (chmod 600) -- used only
                                if the OS keyring is unavailable
        chrome_profiles/<slug>/ isolated Chrome profile per account
        logs/app.log            rolling application log

The app data directory can be overridden with the RMFANTASY_HOME environment
variable (handy for testing or portable installs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_app_dir() -> Path:
    """Return a sensible per-user data directory for the current OS."""
    override = os.environ.get("RMFANTASY_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "RMFantasySMX"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RMFantasySMX"
    # Linux / other: respect XDG if set
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "rmfantasysmx"


APP_DIR: Path = _default_app_dir()
DB_PATH: Path = APP_DIR / "rmfantasy.db"
KEY_PATH: Path = APP_DIR / "secret.key"
CHROME_PROFILES_DIR: Path = APP_DIR / "chrome_profiles"
LOG_DIR: Path = APP_DIR / "logs"
LOG_PATH: Path = LOG_DIR / "app.log"
# Newly-created accounts from the Sign Up tab are appended here as
# ``email:password`` lines (a plaintext convenience export you can copy/paste
# or re-import elsewhere; the encrypted copy still lives in the DB).
SIGNUPS_PATH: Path = APP_DIR / "signups.txt"

# The site we automate.
BASE_URL = "https://www.rmfantasysmx.com/"

# Keyring service/key names (used when OS keyring is available).
KEYRING_SERVICE = "rmfantasysmx-app"
KEYRING_USERNAME = "fernet-master-key"


def ensure_dirs() -> None:
    """Create all required directories if they do not yet exist."""
    for path in (APP_DIR, CHROME_PROFILES_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
