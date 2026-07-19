"""Selenium browser automation for rmfantasysmx.com.

Design principles
-----------------
* One isolated Chrome profile per account (``--user-data-dir``) so sessions
  never collide and stay logged in between runs.
* Login is fully automated (the site has no interactive CAPTCHA): open the
  login modal, fill email/password by input TYPE inside ``#loginModal``, submit,
  and treat the modal closing as the success signal.
* NEVER target Wicket auto-generated ids. Rider dropdowns are found by content
  (``<select>`` elements with 10+ options); the submit button by visible text.
* On later runs the persisted profile is usually already logged in -- detected
  by the presence of enabled rider dropdowns -- so login is skipped.
* AJAX-safe: explicit WebDriverWait everywhere, no raw sleeps for elements.
* Optional per-browser HTTP proxy and anti-bot flags.

Nothing here touches the database; orchestration lives in ``runner.py``.
"""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from . import config, selectors
from .signup import SignupProfile, state_variants

log = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]

_TRANSIENT = (
    TimeoutException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    WebDriverException,
)


class AutomationError(Exception):
    """Base class for automation failures."""


class LoginRequired(AutomationError):
    """Raised when an authenticated session could not be established."""


class NotLoggedIn(AutomationError):
    """Raised (during picks) when an account isn't logged in and we won't try to
    auto-login it (the site's captcha blocks that). The user must log it in first."""


class EligibilityError(AutomationError):
    """Raised when one or more riders are not selectable (out/injured/ineligible)."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "These riders are not available to pick this round (out/ineligible): "
            + ", ".join(missing)
        )


class SubmissionError(AutomationError):
    """Raised when picks could not be submitted or confirmed."""


class SignupError(AutomationError):
    """Raised when a new account could not be registered/confirmed."""


@dataclass
class PickRequest:
    core_five: list[str]   # 5 EXACT roster names, ordered 1st..5th
    wildcard: str          # EXACT roster name for the wildcard slot


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().casefold()


# --------------------------------------------------------------------------- #
# Proxy parsing + authenticated-proxy support
# --------------------------------------------------------------------------- #
def parse_proxy(raw: Optional[str]) -> Optional[dict]:
    """Parse a proxy string into parts.

    Accepts ``host:port`` (open) or ``host:port:user:pass`` (authenticated).
    The password may itself contain ':' (everything after the 3rd ':' is the
    password). Returns ``None`` for blank/malformed input.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) == 2:
        return {"host": parts[0].strip(), "port": parts[1].strip(), "user": None, "pass": None}
    if len(parts) >= 4:
        return {
            "host": parts[0].strip(),
            "port": parts[1].strip(),
            "user": parts[2].strip(),
            "pass": ":".join(parts[3:]).strip(),
        }
    return None


def _chrome_options(profile_dir: str, headless: bool) -> Options:
    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1200,860")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Trim each browser's footprint so hundreds of runs don't exhaust the
    # machine's memory / window handles (the cause of the ~400-browsers wall).
    for flag in (
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
    ):
        opts.add_argument(flag)
    # Suppress Chrome's "Save password?" and "Save address?" bubbles -- they're
    # browser UI that covers the in-page 'Account created' panel, forcing extra
    # clicks. Turning them off lets you just create/log in and hit the button.
    opts.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
        "autofill.profile_enabled": False,
        "autofill.credit_card_enabled": False,
    })
    opts.add_argument("--disable-features=AutofillEnableAccountWalletStorage,PasswordManagerOnboarding")
    if headless:
        opts.add_argument("--headless=new")
    return opts


def _build_wire_driver(profile_dir: str, headless: bool, proxy_info: dict):
    """Create a selenium-wire Chrome that authenticates an upstream proxy.

    selenium-wire runs a local relay Chrome connects to; it forwards traffic to
    the real (authenticated) proxy and supplies the credentials. This works on
    current Chrome, unlike the old proxy-auth extension.
    """
    try:
        from seleniumwire import webdriver as wire_webdriver
    except Exception as exc:  # noqa: BLE001
        raise AutomationError(
            "Authenticated proxy needs 'selenium-wire', but importing it failed:\n"
            f"    {type(exc).__name__}: {exc}\n\n"
            "Fixes (run in the SAME Python that launches the app):\n"
            "  python -m pip install selenium-wire \"blinker<1.8\" setuptools\n"
            "  - 'No module named seleniumwire'  -> not installed / wrong Python\n"
            "  - mentions 'pkg_resources'         -> python -m pip install \"setuptools<81\"\n"
            "  - mentions 'blinker'               -> python -m pip install \"blinker<1.8\"\n"
            "Tip: run  python -m pip show selenium-wire  to confirm where it's installed."
        ) from exc

    from urllib.parse import quote
    user = quote(proxy_info["user"], safe="")
    pw = quote(proxy_info["pass"], safe="")
    proxy_url = f"http://{user}:{pw}@{proxy_info['host']}:{proxy_info['port']}"
    sw_options = {
        "proxy": {
            "http": proxy_url,
            "https": proxy_url,
            "no_proxy": "localhost,127.0.0.1",
        },
        "verify_ssl": False,
    }

    opts = _chrome_options(profile_dir, headless)
    # selenium-wire is a MITM relay; accept its generated cert to avoid warnings.
    opts.add_argument("--ignore-certificate-errors")

    try:
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
        driver = wire_webdriver.Chrome(
            service=service, options=opts, seleniumwire_options=sw_options
        )
    except Exception as exc:  # noqa: BLE001
        log.info("webdriver-manager unavailable for selenium-wire (%s); using Selenium Manager.", exc)
        driver = wire_webdriver.Chrome(options=opts, seleniumwire_options=sw_options)
    driver.set_page_load_timeout(60)
    _apply_stealth(driver)  # same anti-detection as the non-proxy path
    return driver


# --------------------------------------------------------------------------- #
# Driver lifecycle
# --------------------------------------------------------------------------- #
def build_driver(
    profile_dir: str,
    headless: bool = False,
    proxy: Optional[str] = None,
):
    """Create a Chrome driver bound to an isolated profile directory.

    ``proxy`` may be ``host:port`` (open) or ``host:port:user:pass``
    (authenticated -> routed through selenium-wire). ChromeDriver is resolved
    via webdriver-manager when available, otherwise Selenium Manager.
    """
    proxy_info = parse_proxy(proxy)

    # Authenticated proxy: use selenium-wire (modern-Chrome friendly).
    if proxy_info and proxy_info["user"]:
        return _build_wire_driver(profile_dir, headless, proxy_info)

    opts = _chrome_options(profile_dir, headless)
    if proxy_info:  # open proxy, no credentials
        opts.add_argument(f"--proxy-server={proxy_info['host']}:{proxy_info['port']}")

    driver = _new_chrome(opts)
    driver.set_page_load_timeout(60)
    _apply_stealth(driver)
    return driver


def _apply_stealth(driver) -> None:
    """Hide the navigator.webdriver automation flag (best effort).

    Applied on EVERY driver -- including the selenium-wire (proxy) path -- so
    logins/pages behave the same with or without a proxy.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )
    except Exception:  # pragma: no cover - not fatal
        pass


def _new_chrome(opts: Options) -> webdriver.Chrome:
    # Prefer webdriver-manager if installed (per operator preference).
    try:
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)
    except Exception as exc:  # noqa: BLE001
        log.info("webdriver-manager unavailable (%s); using Selenium Manager.", exc)
        return webdriver.Chrome(options=opts)


def persist_login_cookies(driver, days: int = 400) -> int:
    """Make session cookies persistent so the login survives closing Chrome.

    Some sites keep you logged in via a *session* cookie (no expiry), which a
    fresh Selenium profile discards on exit -- so the account looks logged out
    next time even though nothing expired. Re-adding those cookies with a far
    expiry makes Chrome write them to disk. Call right after a successful
    login/signup, while still on the site. Returns how many were converted.
    """
    try:
        cookies = driver.get_cookies()
    except Exception:  # noqa: BLE001
        return 0
    expiry = int(time.time()) + days * 24 * 3600
    converted = 0
    for c in cookies:
        if c.get("expiry"):
            continue  # already persistent
        new = {
            "name": c.get("name"),
            "value": c.get("value"),
            "path": c.get("path", "/"),
            "expiry": expiry,
        }
        if c.get("domain"):
            new["domain"] = c["domain"]
        if c.get("secure"):
            new["secure"] = True
        if c.get("httpOnly"):
            new["httpOnly"] = True
        try:
            driver.add_cookie(new)
            converted += 1
        except Exception:  # noqa: BLE001
            new.pop("domain", None)  # some drivers reject explicit domain
            try:
                driver.add_cookie(new)
                converted += 1
            except Exception:  # noqa: BLE001
                pass
    return converted


def get_public_ip(driver, timeout: int = 25) -> str:
    """Return the browser's public IP (via an echo service) to verify a proxy."""
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            body = driver.find_element(By.TAG_NAME, "body").text.strip()
            if "{" in body and "ip" in body:
                try:
                    return json.loads(body).get("ip", "").strip() or body[:60]
                except Exception:  # noqa: BLE001
                    pass
            if body:
                return body.split()[0][:60]
        except Exception:  # noqa: BLE001
            continue
    return ""


# --------------------------------------------------------------------------- #
# Plain (non-automated) browser launch for manual login
# --------------------------------------------------------------------------- #
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
]


def find_chrome_path() -> Optional[str]:
    """Locate a real Chrome executable for a plain (non-Selenium) launch."""
    for cand in _CHROME_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def build_login_landing(email: str, password: str, site_url: Optional[str] = None) -> str:
    """Write a local HTML page that shows one account's email+password.

    Opened as the first page in that account's Chrome window so the window
    self-identifies (title = email) and hands you the credentials to paste into
    the login (which opens in a new tab). Returns the file path.
    """
    site = site_url or config.BASE_URL
    e_attr, p_attr = html.escape(email), html.escape(password)
    e_js, p_js, site_js = json.dumps(email), json.dumps(password), json.dumps(site)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{e_attr}</title>
<style>
 body{{background:#0b0d12;color:#f2f4f8;font-family:Segoe UI,Arial,sans-serif;padding:36px}}
 .card{{max-width:560px;margin:0 auto;background:#14161f;border:2px solid #2f6bff;border-radius:14px;padding:24px}}
 h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#9aa3b2;font-size:13px;margin-bottom:12px}}
 .warn{{background:#3a1d1d;border:1px solid #b91c1c;color:#fecaca;border-radius:10px;
   padding:12px 14px;font-size:13px;font-weight:600;margin:12px 0 4px}}
 ol{{font-size:13px;color:#cdd3de;line-height:1.7;padding-left:20px;margin:14px 0}}
 label{{font-size:12px;color:#9aa3b2;display:block;margin-top:14px}}
 .row{{display:flex;gap:8px;margin-top:4px}}
 input{{flex:1;background:#0b0d12;color:#fff;border:1px solid #262a36;border-radius:8px;padding:10px;font-size:15px}}
 button{{background:#2f6bff;color:#fff;border:0;border-radius:8px;padding:0 14px;cursor:pointer;font-weight:700}}
 .go{{display:inline-block;margin-top:22px;background:#22c55e;color:#fff;padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:700;border:0;font-size:15px;cursor:pointer}}
 #status{{margin-top:12px;font-size:13px;color:#22c55e;font-weight:600;min-height:18px}}
</style></head><body><div class="card">
 <h1>Log in this account</h1>
 <div class="muted">This window is for ONE account.</div>
 <div class="warn">&#9888; Do NOT type the email &mdash; the site reloads on every keystroke,
  which is why it "refreshes at the 25th character." Use Copy &rarr; Paste instead: pasting
  drops the whole value in at once so it survives the reload.</div>
 <ol>
  <li>Click <b>Open login (email copied)</b> below &mdash; it copies the email and opens the site.</li>
  <li>Click the site's email box and press <b>Ctrl&#8202;+&#8202;V</b> to paste. Let it reload.</li>
  <li>Come back here, click <b>Copy password</b>, then paste it into the site's password box.</li>
  <li>Clear the captcha, click the site's Log In, then close this window.</li>
 </ol>
 <label>Email</label>
 <div class="row"><input id="em" value="{e_attr}" readonly onclick="this.select()">
  <button onclick="cp({e_js},this)">Copy email</button></div>
 <label>Password</label>
 <div class="row"><input id="pw" value="{p_attr}" readonly onclick="this.select()">
  <button onclick="cp({p_js},this)">Copy password</button></div>
 <button class="go" onclick="openLogin()">Open login (email copied) &#8594;</button>
 <div id="status"></div>
 <script>
  function cp(v,b){{try{{navigator.clipboard.writeText(v);var t=b.textContent;b.textContent='Copied';
    setTimeout(function(){{b.textContent=t;}},1200);}}catch(e){{}}}}
  function openLogin(){{
    try{{navigator.clipboard.writeText({e_js});
      document.getElementById('status').textContent='Email copied to clipboard \\u2014 paste it into the site email box (Ctrl+V).';
    }}catch(e){{}}
    window.open({site_js},'_blank','noopener');
  }}
 </script>
</div></body></html>"""
    fd, path = tempfile.mkstemp(prefix="rm_login_", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


def open_profile_browser(profile_dir: str, url: Optional[str] = None) -> None:
    """Launch a NORMAL Chrome window on ``profile_dir`` (no Selenium/automation).

    This lets you log in like a regular person -- the site's captcha sees a real
    browser. The session saves into the profile, and later Selenium pick runs
    reuse it. Raises :class:`AutomationError` if Chrome can't be found.
    """
    exe = find_chrome_path()
    if not exe:
        raise AutomationError(
            "Could not find Chrome. Install Google Chrome (or tell me the path "
            "to chrome.exe)."
        )
    target = url or config.BASE_URL
    # Local files must be passed as file:// URLs.
    if os.path.exists(target):
        target = Path(target).as_uri()
    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        target,
    ]
    subprocess.Popen(args)


def check_login_state(profile_dir: str, headless: bool = True, proxy: Optional[str] = None) -> str:
    """Open the profile briefly and report 'in' / 'out' / 'unknown'.

    Captcha-free (never attempts a login). Round-independent: it does NOT rely on
    editable pick dropdowns (which only exist while a round is open), so a
    logged-in account between rounds still reads 'in'. Returns 'unknown' when it
    can't tell, so callers can leave the account's status untouched.
    """
    with chrome_session(profile_dir, headless=headless, proxy=proxy) as driver:
        driver.get(config.BASE_URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass
        time.sleep(1.0)
        return login_state(driver)


def session_is_live(profile_dir: str, headless: bool = True, proxy: Optional[str] = None) -> bool:
    """Back-compat wrapper: True only when clearly logged in."""
    return check_login_state(profile_dir, headless=headless, proxy=proxy) == "in"


def profile_has_session(profile_dir: str) -> str:
    """SUPER-FAST login check: read the profile's cookie DB directly (no browser).

    Returns 'in' if a persistent rmfantasysmx cookie exists (a saved login),
    'out' if the site has been visited but has no cookies, or 'unknown' when we
    can't tell (so the caller leaves the account's flag untouched). This is
    ~instant per account -- no Chrome launch.
    """
    import sqlite3

    candidates = [
        os.path.join(profile_dir, "Default", "Network", "Cookies"),
        os.path.join(profile_dir, "Default", "Cookies"),
        os.path.join(profile_dir, "Network", "Cookies"),
        os.path.join(profile_dir, "Cookies"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        return "unknown"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        try:
            persistent = con.execute(
                "SELECT COUNT(*) FROM cookies "
                "WHERE host_key LIKE '%rmfantasysmx%' AND is_persistent=1"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 - older schema without is_persistent
            persistent = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%rmfantasysmx%'"
            ).fetchone()[0]
        any_site = con.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%rmfantasysmx%'"
        ).fetchone()[0]
    except Exception:  # noqa: BLE001
        return "unknown"
    finally:
        con.close()

    if persistent > 0:
        return "in"
    if any_site == 0:
        return "out"
    return "unknown"


def _kill_pid_tree_windows(pid: int) -> None:
    """Kill a process and ALL its descendants on Windows via taskkill /T."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def _hard_close(driver) -> None:
    """Quit the driver AND guarantee its entire Chrome process tree is gone.

    THE ~400-BROWSER WALL: chromedriver launches chrome.exe, which in turn
    spawns a tree of renderer/GPU child processes. Killing only chromedriver
    (what we used to do) leaves that whole Chrome tree ORPHANED. Over a long
    run those orphans pile up and exhaust the machine's RAM / handles, so new
    Chrome instances stop launching -- which surfaces as browsers that "open
    then close right away" and an 'Unexpected error' a few hundred accounts in.

    Fix: snapshot the full process tree before quitting, quit gracefully (so
    the session/cookies flush to the profile), then reap every survivor.
    """
    svc = getattr(driver, "service", None)
    proc = getattr(svc, "process", None) if svc is not None else None
    driver_pid = getattr(proc, "pid", None)

    # Snapshot the full tree (chromedriver -> chrome.exe -> renderers) up front
    # so we can reap orphans regardless of how quit() behaves. psutil verifies
    # pid+create_time, so it won't kill a recycled PID by mistake.
    ps_tree = []
    if driver_pid is not None:
        try:
            import psutil

            root = psutil.Process(driver_pid)
            ps_tree = root.children(recursive=True) + [root]
        except Exception:  # noqa: BLE001 - psutil missing or process already gone
            ps_tree = []

    # Graceful close first: lets Chrome flush the session/cookies to disk so the
    # account stays logged in next time.
    try:
        driver.quit()
    except Exception:  # noqa: BLE001
        pass

    if ps_tree:
        # Reap any member of the tree that survived quit().
        try:
            import psutil

            alive = [p for p in ps_tree if p.is_running()]
            for p in alive:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                psutil.wait_procs(alive, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
    else:
        # No psutil: best-effort OS-level tree kill. If quit() hung, the tree is
        # still intact and taskkill /T reaps chrome.exe + renderers too.
        if driver_pid is not None and sys.platform.startswith("win"):
            _kill_pid_tree_windows(driver_pid)
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def chrome_session(profile_dir: str, headless: bool = False, proxy: Optional[str] = None):
    driver = build_driver(profile_dir, headless=headless, proxy=proxy)
    try:
        yield driver
    finally:
        _hard_close(driver)


# --------------------------------------------------------------------------- #
# Waiting helpers (custom OR-wait; EC.or_ is unreliable across versions)
# --------------------------------------------------------------------------- #
def wait_until_any(driver, predicates: Iterable[Callable], timeout: float = 20, poll: float = 0.4):
    """Return the first truthy predicate result, or raise TimeoutException."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        for pred in predicates:
            try:
                result = pred(driver)
                if result:
                    return result
            except (NoSuchElementException, StaleElementReferenceException):
                continue
        time.sleep(poll)
    raise TimeoutException("None of the wait conditions became true in time.")


def _visible_elements(driver, css: str):
    out = []
    try:
        els = driver.find_elements(By.CSS_SELECTOR, css)
    except Exception:  # noqa: BLE001
        return out
    for el in els:
        try:
            if el.is_displayed():
                out.append(el)
        except StaleElementReferenceException:
            continue
    return out


def _find_first_visible(driver, css_list: Iterable[str]):
    for css in css_list:
        if not css:
            continue
        for el in _visible_elements(driver, css):
            return el
    return None


# --------------------------------------------------------------------------- #
# Rider dropdown discovery (content-based, id-free)
# --------------------------------------------------------------------------- #
def find_rider_selects(driver) -> list:
    """Return rider ``<select>`` WebElements (those with 10+ options), in order."""
    result = []
    for el in driver.find_elements(By.TAG_NAME, "select"):
        try:
            option_count = len(el.find_elements(By.TAG_NAME, "option"))
        except StaleElementReferenceException:
            continue
        if option_count >= selectors.MIN_RIDER_OPTIONS:
            result.append(el)
    return result


def _option_names(select_el) -> dict[str, str]:
    """Map normalized option text -> exact visible text (skipping placeholders).

    Stale-tolerant: returns {} if the <select> is being re-rendered (AJAX), so
    callers can wait and retry instead of crashing.
    """
    out: dict[str, str] = {}
    try:
        options = Select(select_el).options
    except StaleElementReferenceException:
        return out
    for opt in options:
        try:
            text = (opt.text or "").strip()
        except StaleElementReferenceException:
            continue
        if _norm(text) in selectors.PLACEHOLDER_OPTION_TEXTS:
            continue
        if text:
            out.setdefault(_norm(text), text)
    return out


def get_round_label(driver) -> str:
    """Best-effort read of the current round title (e.g. 'Round #7 - Spring Creek')."""
    for css in ("h1", "h2", ".roundTitle", ".pickHeader"):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, css)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                txt = " ".join((el.text or "").split())
            except StaleElementReferenceException:
                continue
            if txt and "round" in txt.lower():
                return txt
    return "Current round"


def scrape_roster(driver) -> list[str]:
    """Scrape the unique rider names from the pick-page dropdowns.

    Riders appear twice per dropdown (featured + alphabetical); we de-duplicate
    and return them sorted for stable display.
    """
    selects = find_rider_selects(driver)
    if not selects:
        raise AutomationError(
            "No rider dropdowns found. Are you logged in and on the picks page?"
        )
    names: dict[str, str] = {}
    for sel in selects:
        for norm, text in _option_names(sel).items():
            names.setdefault(norm, text)
    return sorted(names.values(), key=str.casefold)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def _modal_visible(driver) -> bool:
    for el in driver.find_elements(By.CSS_SELECTOR, selectors.LOGIN_MODAL_CSS):
        if el.is_displayed():
            return True
    return False


def is_logged_in(driver) -> bool:
    """Logged in == at least one ENABLED rider dropdown is present.

    Guests see the dropdowns disabled; authenticated users can edit them.
    """
    for el in find_rider_selects(driver):
        try:
            if el.is_enabled() and el.get_attribute("disabled") is None:
                return True
        except StaleElementReferenceException:
            continue
    return False


def login_state(driver) -> str:
    """Return 'in' (logged in), 'out' (guest), or 'unknown' (can't tell).

    Round-independent: does NOT depend on editable pick dropdowns (those only
    exist while a round is open). Uses guest markers instead, so an account that
    is logged in between rounds still reads 'in'. 'unknown' is returned when the
    page didn't load properly, so callers can leave the account untouched rather
    than wrongly flipping it to logged-out.
    """
    # Editable rider dropdowns = definitely logged in (only true while open).
    try:
        if is_logged_in(driver):
            return "in"
    except Exception:  # noqa: BLE001
        pass
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:  # noqa: BLE001
        return "unknown"
    low = " ".join((body or "").split()).casefold()
    if len(low) < 200:
        return "unknown"   # page didn't really render
    guest_markers = [
        "sign up or log in",
        "log in to see your",
        "are you a new player",
        "have an existing account",
        "sign up or log in to submit",
    ]
    if any(g in low for g in guest_markers):
        return "out"
    # Page loaded and shows no guest prompts -> treat as logged in.
    return "in"


def _click(driver, el) -> None:
    try:
        el.click()
        return
    except Exception:  # noqa: BLE001 - fall back to a JS click
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
    except Exception:  # noqa: BLE001 - stale/gone; caller stays resilient
        pass


def _click_by_text(driver, texts: Iterable[str], scope_css: Optional[str] = None) -> bool:
    prefix = ""
    if scope_css:
        # Limit to descendants of the scope element.
        pass
    for text in texts:
        xpath = (
            f"//button[normalize-space()='{text}'] | "
            f"//a[normalize-space()='{text}'] | "
            f"//input[(@type='submit' or @type='button') and @value='{text}']"
        )
        for el in driver.find_elements(By.XPATH, xpath):
            if el.is_displayed() and el.is_enabled():
                _click(driver, el)
                return True
    return False


def do_login(driver, email: str, password: str, timeout: int = 30) -> None:
    """Perform an automated login. Raises LoginRequired on failure."""
    # Open the modal.
    link = _find_first_visible(driver, [selectors.LOGIN_LINK_CSS])
    if link is not None:
        _click(driver, link)

    # Wait for the modal + email field.
    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS))
        )
    except TimeoutException as exc:
        raise LoginRequired("Login modal / email field did not appear.") from exc

    # The site fires a Wicket AJAX refresh when the EMAIL field changes, which
    # re-renders (and staleifies) the password field. Two things matter:
    #   1) Set the email in ONE shot via JS. send_keys types char-by-char, and
    #      because the field also listens on keyup/input, that fires a refresh
    #      on EVERY keystroke -- a refresh storm that wipes the field mid-type.
    #      One JS assignment + a single change event = exactly one refresh.
    #   2) After that single change, WAIT for the refresh to actually land (the
    #      old password node goes stale) before re-finding + filling password
    #      on the settled DOM.
    def _fill_login_fields() -> None:
        email_el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS))
        )
        # Grab a node the refresh will replace, so we can detect it completed.
        try:
            marker = driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS)
        except NoSuchElementException:
            marker = None

        _set_input_value(driver, email_el, email)  # one-shot value + one change

        # Wait for the email-triggered refresh to land, then let it settle.
        if marker is not None:
            try:
                WebDriverWait(driver, 8).until(EC.staleness_of(marker))
            except TimeoutException:
                pass  # no refresh happened (or already finished) -- that's fine
        time.sleep(0.4)  # give the re-rendered fields a beat to attach handlers

        # Fill the password on the fresh (post-refresh) DOM.
        pwd = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS))
        )
        _set_input_value(driver, pwd, password)

        # Restore the email if the refresh cleared it -- via JS value only, so
        # we don't kick off yet another change/refresh cycle.
        em = driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS)
        if not (em.get_attribute("value") or "").strip():
            driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                em, email,
            )
        # A late refresh can wipe the password after we set it -> force a retry.
        pwd = driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS)
        if not (pwd.get_attribute("value") or "").strip():
            raise StaleElementReferenceException("password cleared by a late form refresh")

    for _ in range(4):
        try:
            _fill_login_fields()
            break
        except StaleElementReferenceException:
            time.sleep(0.6)

    # Submit: re-find fresh (the form may have re-rendered again).
    submit = _find_first_visible(driver, [selectors.LOGIN_SUBMIT_CSS])
    if submit is not None:
        _click(driver, submit)
    elif not _click_by_text(driver, selectors.LOGIN_SUBMIT_TEXTS):
        # Last resort: submit the form via the password field.
        try:
            driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS).submit()
        except Exception:  # noqa: BLE001
            pass

    # Success == modal closes OR rider dropdowns become enabled.
    try:
        wait_until_any(
            driver,
            [
                lambda d: not _modal_visible(d),
                lambda d: is_logged_in(d),
            ],
            timeout=timeout,
        )
    except TimeoutException:
        err = _find_first_visible(driver, [selectors.LOGIN_ERROR_CSS])
        detail = f" Site said: {err.text.strip()}" if err and err.text.strip() else ""
        raise LoginRequired(f"Login did not complete (modal stayed open).{detail}")

    # Give the page a beat to render the (now editable) pick form.
    try:
        WebDriverWait(driver, 10).until(lambda d: is_logged_in(d))
    except TimeoutException:
        # Modal closed but dropdowns not yet enabled; reload once.
        driver.get(config.BASE_URL)
        WebDriverWait(driver, 15).until(lambda d: is_logged_in(d))

    # Keep the login after the browser closes (in case it's a session cookie).
    persist_login_cookies(driver)


def ensure_logged_in(
    driver,
    email: str,
    password: str,
    login_timeout: int = 30,
    status_cb: Optional[StatusCallback] = None,
    attempt_login: bool = True,
) -> None:
    """Guarantee an authenticated session (reuse saved session, else log in).

    With ``attempt_login=False`` (used for picks) it will NOT try to auto-login
    a logged-out account -- the site's captcha blocks that -- and instead raises
    :class:`NotLoggedIn` with a clear message telling you to log it in first.
    """
    driver.get(config.BASE_URL)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    if is_logged_in(driver):
        if status_cb:
            status_cb("Already logged in (reused session).")
        return

    if not attempt_login:
        raise NotLoggedIn("Not logged in (auto-login disabled for this run).")

    # Auto-login works on this site during picks, so do it.
    if status_cb:
        status_cb("Logging in...")
    do_login(driver, email, password, timeout=login_timeout)
    if status_cb:
        status_cb("Login OK.")


# --------------------------------------------------------------------------- #
# Pick selection & submission
# --------------------------------------------------------------------------- #
def check_eligibility(selects: list, request: PickRequest) -> list[str]:
    """Return requested riders NOT present in their target dropdown's options."""
    missing: list[str] = []
    if len(selects) < selectors.EXPECTED_RIDER_SELECTS:
        raise SubmissionError(
            f"Expected {selectors.EXPECTED_RIDER_SELECTS} rider dropdowns, "
            f"found {len(selects)}. The pick page may not be loaded."
        )

    for idx, rider in enumerate(request.core_five):
        rider = (rider or "").strip()
        options = _option_names(selects[idx])
        if not rider:
            missing.append(f"(empty place {idx + 1})")
        elif _norm(rider) not in options:
            missing.append(rider)

    wc = (request.wildcard or "").strip()
    wc_options = _option_names(selects[selectors.WILDCARD_SELECT_INDEX])
    if not wc or _norm(wc) not in wc_options:
        missing.append(wc or "(empty wildcard)")

    seen, unique = set(), []
    for m in missing:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique


def _select_rider(select_el, rider: str) -> None:
    options = _option_names(select_el)
    key = _norm(rider)
    if key not in options:
        raise EligibilityError([rider])
    # select_by_visible_text picks the first occurrence (featured section) when
    # a rider is listed twice -- which is exactly what we want.
    Select(select_el).select_by_visible_text(options[key])


def _select_rider(select_el, rider: str) -> None:
    options = _option_names(select_el)
    key = _norm(rider)
    if key not in options:
        raise EligibilityError([rider])
    # select_by_visible_text picks the first occurrence (featured section) when
    # a rider is listed twice -- which is exactly what we want.
    Select(select_el).select_by_visible_text(options[key])


def select_all_riders(driver, request: PickRequest) -> None:
    selects = find_rider_selects(driver)
    missing = check_eligibility(selects, request)
    if missing:
        raise EligibilityError(missing)
    for idx, rider in enumerate(request.core_five):
        _select_rider(selects[idx], rider)
    _select_rider(selects[selectors.WILDCARD_SELECT_INDEX], request.wildcard)


def click_submit(driver) -> None:
    if _click_by_text(driver, selectors.SUBMIT_PICKS_BUTTON_TEXTS):
        return
    el = _find_first_visible(driver, [selectors.SUBMIT_PICKS_BUTTON_CSS])
    if el is None:
        raise SubmissionError(
            "Could not find the submit-picks button. Confirm "
            "SUBMIT_PICKS_BUTTON_TEXTS / SUBMIT_PICKS_BUTTON_CSS in selectors.py."
        )
    _click(driver, el)


def confirm_success(driver, timeout: int = 20) -> bool:
    success_texts = [t.casefold() for t in selectors.SUBMIT_SUCCESS_TEXT_CONTAINS]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_first_visible(driver, selectors.SUBMIT_SUCCESS_CSS) is not None:
            return True
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.casefold()
            if any(t in body for t in success_texts):
                return True
        except _TRANSIENT:
            pass
        time.sleep(0.8)
    return False


def submit_picks(
    driver,
    request: PickRequest,
    status_cb: Optional[StatusCallback] = None,
    verify: bool = True,
) -> None:
    """Select the core five + wildcard and submit, on a logged-in pick page."""
    if status_cb:
        status_cb("Selecting riders...")
    select_all_riders(driver, request)

    if status_cb:
        status_cb("Submitting...")
    click_submit(driver)

    if not verify:
        return
    if status_cb:
        status_cb("Confirming...")
    if not confirm_success(driver):
        raise SubmissionError(
            "Submitted but no confirmation detected. Verify SUBMIT_SUCCESS_* "
            "selectors in selectors.py or check the site."
        )



# --------------------------------------------------------------------------- #
# Sign up / registration
# --------------------------------------------------------------------------- #
def _is_visible(el) -> bool:
    try:
        return el.is_displayed()
    except Exception:  # noqa: BLE001
        return False


def _signup_scope(driver):
    """Return the signup modal element if visible, else the whole driver."""
    el = _find_first_visible(driver, [selectors.SIGNUP_MODAL_CSS])
    return el or driver


def _find_input_by_placeholder(scope, placeholder_substrings):
    """First visible <input>/<textarea> whose placeholder contains any substring."""
    subs = [s.casefold() for s in placeholder_substrings]
    try:
        elements = scope.find_elements(By.CSS_SELECTOR, "input, textarea")
    except Exception:  # noqa: BLE001
        return None
    for el in elements:
        try:
            if not _is_visible(el):
                continue
            ph = (el.get_attribute("placeholder") or "").casefold()
        except StaleElementReferenceException:
            continue
        if ph and any(s in ph for s in subs):
            return el
    return None


def _set_input_value(driver, el, value: str) -> None:
    """Set an input's value in a SINGLE shot, then fire the events frameworks
    listen for.

    Unlike ``send_keys`` (which types character-by-character and, on a field
    with a keyup/input AJAX handler, triggers a form refresh on every keystroke),
    this assigns the whole value at once so at most ONE change/refresh fires.
    Raises on a stale element so the caller can retry on the re-rendered DOM.
    """
    driver.execute_script(
        "arguments[0].focus();"
        "arguments[0].value='';"
        "arguments[0].value=arguments[1];"
        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('blur',{bubbles:true}));",
        el, value,
    )


def _fill_element(driver, el, value: str) -> None:
    try:
        el.clear()
    except Exception:  # noqa: BLE001
        pass
    try:
        el.send_keys(value)
    except Exception:  # noqa: BLE001 - element went stale mid-fill; skip
        return
    # Fire the events client-side frameworks listen for, so validation runs and
    # a "disabled until valid" Submit button becomes enabled.
    try:
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('keyup', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('blur', {bubbles:true}));",
            el,
        )
    except Exception:  # noqa: BLE001
        pass


def _find_input_by_label(driver, scope, label_substrings):
    """Find an input/textarea via its associated <label> text.

    Tries, in order: the label's ``for`` target, an input nested in the label,
    an input in the label's parent container, then the next input in the DOM.
    """
    subs = [s.casefold() for s in label_substrings]
    for lbl in scope.find_elements(By.TAG_NAME, "label"):
        try:
            txt = " ".join((lbl.text or "").split()).casefold()
        except StaleElementReferenceException:
            continue
        if not txt or not any(s in txt for s in subs):
            continue
        fid = lbl.get_attribute("for")
        if fid:
            try:
                el = driver.find_element(By.ID, fid)
                if el.tag_name in ("input", "textarea") and _is_visible(el):
                    return el
            except NoSuchElementException:
                pass
        for el in lbl.find_elements(By.CSS_SELECTOR, "input, textarea"):
            if _is_visible(el):
                return el
        try:
            parent = lbl.find_element(By.XPATH, "..")
            for el in parent.find_elements(By.CSS_SELECTOR, "input, textarea"):
                if _is_visible(el):
                    return el
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        for el in lbl.find_elements(By.XPATH, "following::input[1] | following::textarea[1]"):
            if _is_visible(el):
                return el
    return None


def _describe_inputs(scope, limit: int = 25) -> list[str]:
    """Compact description of visible form fields, for actionable errors."""
    out: list[str] = []
    for el in scope.find_elements(By.CSS_SELECTOR, "input, textarea, select"):
        try:
            if not _is_visible(el):
                continue
            tag = el.tag_name
            typ = (el.get_attribute("type") or "").strip()
            ph = (el.get_attribute("placeholder") or "").strip()
            name = (el.get_attribute("name") or "").strip()
            out.append(f"{tag}[type={typ or '-'} ph='{ph}' name='{name}']")
        except StaleElementReferenceException:
            continue
        if len(out) >= limit:
            break
    return out


def _fill_field(driver, scope, placeholders, labels, value, field_name, required=True) -> bool:
    """Fill a field found by placeholder first, then by its <label> text."""
    el = _find_input_by_placeholder(scope, placeholders)
    if el is None:
        el = _find_input_by_label(driver, scope, labels)
    if el is None:
        if required:
            fields = ", ".join(_describe_inputs(scope)) or "(no visible fields)"
            raise SignupError(
                f"Could not find the '{field_name}' field on the sign-up form. "
                f"The visible fields I saw were: [{fields}]. Share this so the "
                f"SIGNUP_* selectors can be set exactly."
            )
        return False
    _fill_element(driver, el, value)
    return True


def _visible_selects(scope):
    return [s for s in scope.find_elements(By.TAG_NAME, "select") if _is_visible(s)]


def _select_has_option(select_el, needles) -> bool:
    ns = [n.casefold() for n in needles]
    try:
        for opt in select_el.find_elements(By.TAG_NAME, "option"):
            t = (opt.text or "").strip().casefold()
            if t and any(n == t or n in t for n in ns):
                return True
    except StaleElementReferenceException:
        return False
    return False


def _find_select_with_option(scope, needles):
    for s in _visible_selects(scope):
        if _select_has_option(s, needles):
            return s
    return None


def _select_option_tolerant(select_el, wanted_variants) -> bool:
    """Select the first option matching any variant (exact, then contains)."""
    try:
        sel = Select(select_el)
        options = sel.options
        for variant in wanted_variants:
            v = variant.casefold()
            for o in options:
                if (o.text or "").strip().casefold() == v:
                    sel.select_by_visible_text(o.text)
                    return True
        for variant in wanted_variants:
            v = variant.casefold()
            for o in options:
                if v and v in (o.text or "").strip().casefold():
                    sel.select_by_visible_text(o.text)
                    return True
    except Exception:  # noqa: BLE001 - stale/detached select; treat as not-set
        return False
    return False


def _check_age_radio(driver, label_substrings) -> bool:
    """Tick the '18 years or older' radio, located via its label text."""
    for phrase in label_substrings:
        p = phrase.lower()
        xp = (
            "//label[contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), "
            f"'{p}')]"
        )
        for lbl in driver.find_elements(By.XPATH, xp):
            try:
                if not _is_visible(lbl):
                    continue
                fid = lbl.get_attribute("for")
                if fid:
                    els = driver.find_elements(By.ID, fid)
                    if els:
                        _click(driver, els[0])
                        return True
                inside = lbl.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if inside:
                    _click(driver, inside[0])
                    return True
                near = lbl.find_elements(By.XPATH, "preceding::input[@type='radio'][1]")
                if near:
                    _click(driver, near[0])
                    return True
            except StaleElementReferenceException:
                continue
    return False


def _signup_form_present(driver) -> bool:
    """True when the registration form is on-screen.

    Distinguishes the signup form from the login modal: signup has a
    First name / Nickname / Confirm field, OR two visible password inputs
    (password + confirm) -- login only ever shows one.
    """
    scope = _signup_scope(driver)
    strong = (
        selectors.SIGNUP_FIRST_NAME_PLACEHOLDERS
        + selectors.SIGNUP_NICKNAME_PLACEHOLDERS
        + selectors.SIGNUP_CONFIRM_PLACEHOLDERS
    )
    if _find_input_by_placeholder(scope, strong) is not None:
        return True
    pwds = [e for e in scope.find_elements(By.CSS_SELECTOR, "input[type='password']") if _is_visible(e)]
    return len(pwds) >= 2


def _visible_click_texts(driver, limit: int = 14) -> list[str]:
    """Short, visible button/link labels -- used to explain a failed open."""
    out: list[str] = []
    for tag in ("button", "a", "input"):
        for el in driver.find_elements(By.TAG_NAME, tag):
            try:
                if not el.is_displayed():
                    continue
                raw = el.get_attribute("value") if tag == "input" else el.text
                t = " ".join((raw or "").split())
                if t and len(t) <= 44 and t not in out:
                    out.append(t)
            except StaleElementReferenceException:
                continue
            if len(out) >= limit:
                return out
    return out


def _signup_open_candidates(driver) -> list:
    """Visible elements that look like a 'Sign Up' trigger (shortest first)."""
    subs = [s.casefold() for s in selectors.SIGNUP_OPEN_TEXTS] + [
        "sign up", "signup", "register", "create account", "new player",
    ]
    exclude = ("log in", "login", "existing account", "sign in")
    cands: list[tuple[int, object]] = []
    seen: set[tuple[str, str]] = set()
    for tag in ("button", "a", "input", "span", "div", "li"):
        for el in driver.find_elements(By.TAG_NAME, tag):
            try:
                if not el.is_displayed():
                    continue
                raw = el.get_attribute("value") if tag == "input" else el.text
                t = " ".join((raw or "").split())
                low = t.casefold()
                if not low or len(low) > 44:
                    continue
                if any(x in low for x in exclude):
                    continue
                if any(s in low for s in subs):
                    key = (tag, t)
                    if key in seen:
                        continue
                    seen.add(key)
                    cands.append((len(low), el))
            except StaleElementReferenceException:
                continue
    cands.sort(key=lambda pair: pair[0])
    return [el for _, el in cands]


def _open_signup_modal(driver, timeout: int = 30) -> bool:
    """Click a 'Sign Up' trigger and wait for the registration form.

    Tries each candidate (shortest label first), giving the modal a moment to
    render after each click. Returns True once the form is present.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _signup_form_present(driver):
            return True
        for el in _signup_open_candidates(driver):
            try:
                _click(driver, el)
            except Exception:  # noqa: BLE001
                continue
            try:
                WebDriverWait(driver, 4).until(_signup_form_present)
                return True
            except TimeoutException:
                continue
        time.sleep(0.5)
    return _signup_form_present(driver)


def _find_success_popup(driver):
    """The post-signup success popup (e.g. the 'RM Cash' welcome), if visible.

    Must be a visible popup-ish container that is NOT the signup form (no
    password field inside) AND contains a success keyword -- so we don't
    mistake the signup modal or static page text for it.
    """
    keys = [t.casefold() for t in selectors.SIGNUP_SUCCESS_TEXT_CONTAINS]
    for css in selectors.SIGNUP_SUCCESS_POPUP_CSS:
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if not el.is_displayed():
                    continue
                if el.find_elements(By.CSS_SELECTOR, "input[type='password']"):
                    continue
                txt = (el.text or "").casefold()
                if txt and any(k in txt for k in keys):
                    return el
            except StaleElementReferenceException:
                continue
    return None


def _dismiss_success_popup(driver, popup) -> None:
    """Close the success popup via its X / close control (best effort)."""
    close_texts = [t.casefold() for t in selectors.SIGNUP_POPUP_CLOSE_TEXTS]
    try:
        for el in popup.find_elements(By.CSS_SELECTOR, "button, a, span, i, div"):
            try:
                if el.is_displayed() and " ".join((el.text or "").split()).casefold() in close_texts:
                    _click(driver, el)
                    return
            except StaleElementReferenceException:
                continue
    except Exception:  # noqa: BLE001
        pass
    for css in selectors.SIGNUP_POPUP_CLOSE_CSS:
        el = _find_first_visible(driver, [css])
        if el is not None:
            _click(driver, el)
            return


def _confirm_signup(driver, timeout: int = 30, status_cb: Optional[StatusCallback] = None) -> bool:
    """Strictly confirm a REAL signup before we keep the account.

    Success == the post-signup popup (e.g. RM Cash) appears, OR the site logs
    you in (rider dropdowns become editable). The weak "the form disappeared"
    signal is intentionally NOT used, so accounts that were never actually
    created don't get saved.
    """
    say = status_cb or (lambda _m: None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        popup = _find_success_popup(driver)
        if popup is not None:
            say("Success popup detected (RM Cash) - closing it.")
            _dismiss_success_popup(driver, popup)
            return True
        if is_logged_in(driver):
            return True
        time.sleep(0.5)
    # Final check after the wait window.
    if _find_success_popup(driver) is not None or is_logged_in(driver):
        return True
    return False


def _scroll_and_click(driver, el) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    except Exception:  # noqa: BLE001
        pass
    # If the control is gated 'disabled' (client-side until valid), un-gate it
    # so the click can go through.
    try:
        driver.execute_script(
            "arguments[0].removeAttribute('disabled');"
            "if(arguments[0].classList){arguments[0].classList.remove('disabled');}",
            el,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click()
        return
    except _TRANSIENT:
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
    except Exception:  # noqa: BLE001
        pass


def dump_signup_form(driver) -> str:
    """Return a detailed dump of the signup form's buttons + fields.

    Used by the 'Debug form' button so the exact Submit-button markup can be
    shared and the selectors set precisely.
    """
    scope = _signup_scope(driver)
    lines: list[str] = ["=== BUTTONS / CLICKABLES ==="]
    for tag in ("button", "input", "a"):
        for el in scope.find_elements(By.TAG_NAME, tag):
            try:
                if not el.is_displayed():
                    continue
                typ = (el.get_attribute("type") or "").strip()
                if tag == "input" and typ not in ("submit", "button", "image", ""):
                    continue
                txt = " ".join(((el.get_attribute("value") if tag == "input" else el.text) or "").split())
                oh = " ".join((el.get_attribute("outerHTML") or "").split())[:220]
                lines.append(
                    f"<{tag}> type={typ or '-'} enabled={el.is_enabled()} "
                    f"class={el.get_attribute('class') or ''!r} text={txt!r}"
                )
                lines.append(f"    {oh}")
            except Exception:  # noqa: BLE001
                continue
    lines.append("")
    lines.append("=== INPUT / SELECT FIELDS ===")
    for el in scope.find_elements(By.CSS_SELECTOR, "input, textarea, select"):
        try:
            if not el.is_displayed():
                continue
            lines.append(
                f"<{el.tag_name}> type={el.get_attribute('type') or '-'} "
                f"ph={el.get_attribute('placeholder') or ''!r} "
                f"name={el.get_attribute('name') or ''!r} id={el.get_attribute('id') or ''!r}"
            )
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(lines)


def _element_label(el) -> str:
    try:
        raw = el.get_attribute("value") if el.tag_name == "input" else el.text
        return " ".join((raw or "").split())[:30]
    except Exception:  # noqa: BLE001
        return ""


def _find_submit_button(driver):
    """Locate the registration Submit control.

    Priority: explicit CSS -> EXACT text match on a real button/link/input
    (the approach that worked previously) -> a styled element whose text
    *contains* 'submit'. We deliberately do NOT match 'sign up' here so we
    can't accidentally re-click the header 'SIGN UP' trigger.
    """
    el = _find_first_visible(driver, [selectors.SIGNUP_SUBMIT_CSS])
    if el is not None:
        return el

    scope = _signup_scope(driver)
    roots = [scope, driver] if scope is not driver else [driver]

    # Exact text match on real controls, in PRIORITY order (so 'Submit' wins
    # over 'Sign Up' -- we must not re-click the header 'SIGN UP' trigger).
    for want in [t.casefold() for t in selectors.SIGNUP_SUBMIT_TEXTS]:
        for root in roots:
            for tag in ("button", "input", "a"):
                for e in root.find_elements(By.TAG_NAME, tag):
                    try:
                        if not e.is_displayed() or not e.is_enabled():
                            continue
                        raw = e.get_attribute("value") if tag == "input" else e.text
                        if " ".join((raw or "").split()).casefold() == want:
                            return e
                    except StaleElementReferenceException:
                        continue

    # Fallback: any visible element whose text contains 'submit'.
    exclude = ("log in", "login", "sign in", "cancel", "close", "reset", "back")
    for root in roots:
        cands: list[tuple[int, object]] = []
        for tag in ("button", "input", "a", "span", "div"):
            for e in root.find_elements(By.TAG_NAME, tag):
                try:
                    if not e.is_displayed():
                        continue
                    raw = e.get_attribute("value") if tag == "input" else e.text
                    low = " ".join((raw or "").split()).casefold()
                    if not low or len(low) > 24 or any(x in low for x in exclude):
                        continue
                    if "submit" in low:
                        cands.append((len(low), e))
                except StaleElementReferenceException:
                    continue
        cands.sort(key=lambda pair: pair[0])
        if cands:
            return cands[0][1]
    return None


def _click_signup_submit(driver, status_cb: Optional[StatusCallback] = None) -> bool:
    """Click the Submit control and report exactly what was clicked."""
    say = status_cb or (lambda _m: None)
    el = _find_submit_button(driver)
    if el is None:
        say("Could not find a Submit button to click.")
        return False
    label = _element_label(el)
    was_disabled = False
    try:
        was_disabled = el.get_attribute("disabled") is not None or not el.is_enabled()
    except Exception:  # noqa: BLE001
        pass
    _scroll_and_click(driver, el)
    say(f"Clicked Submit ('{label or el.tag_name}'{', was disabled' if was_disabled else ''}).")
    return True


def _is_fatal_signup_error(text: str) -> bool:
    t = (text or "").casefold()
    return any(k in t for k in selectors.SIGNUP_FATAL_ERROR_CONTAINS)


def _open_and_fill(driver, profile: SignupProfile, say, timeout: int) -> None:
    """Open the registration modal and fill every field + tick 18+."""
    driver.get(config.BASE_URL)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    # Wait for the page to actually finish loading (slow residential proxies)
    # before hunting for the Sign Up button.
    try:
        WebDriverWait(driver, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass
    time.sleep(1.5)

    say("Opening sign-up form...")
    if not _open_signup_modal(driver, timeout=timeout):
        seen = _visible_click_texts(driver)
        hint = (", ".join(seen) if seen else "(no buttons detected)")
        raise SignupError(
            "Could not open the sign-up form. The buttons/links I could see were: "
            f"[{hint}]. (If the site was still loading, try a lower concurrency "
            "or a faster proxy.)"
        )

    scope = _signup_scope(driver)
    say("Filling registration form...")
    _fill_field(driver, scope, selectors.SIGNUP_FIRST_NAME_PLACEHOLDERS, selectors.SIGNUP_FIRST_NAME_LABELS, profile.first_name, "first name")
    _fill_field(driver, scope, selectors.SIGNUP_LAST_NAME_PLACEHOLDERS, selectors.SIGNUP_LAST_NAME_LABELS, profile.last_name, "last name")
    _fill_field(driver, scope, selectors.SIGNUP_EMAIL_PLACEHOLDERS, selectors.SIGNUP_EMAIL_LABELS, profile.email, "email")
    _fill_field(driver, scope, selectors.SIGNUP_PHONE_PLACEHOLDERS, selectors.SIGNUP_PHONE_LABELS, profile.phone, "phone", required=False)

    # Mailing address (city/state/postal from your input; street is random).
    if profile.street:
        _fill_field(driver, scope, selectors.SIGNUP_STREET_PLACEHOLDERS, selectors.SIGNUP_STREET_LABELS, profile.street, "street", required=False)
    if profile.city:
        _fill_field(driver, scope, selectors.SIGNUP_CITY_PLACEHOLDERS, selectors.SIGNUP_CITY_LABELS, profile.city, "city", required=False)
    if profile.postal_code:
        _fill_field(driver, scope, selectors.SIGNUP_POSTAL_PLACEHOLDERS, selectors.SIGNUP_POSTAL_LABELS, profile.postal_code, "postal code", required=False)
    _fill_field(driver, scope, selectors.SIGNUP_NICKNAME_PLACEHOLDERS, selectors.SIGNUP_NICKNAME_LABELS, profile.nickname, "nickname", required=False)

    # Country (defaults to United States) + State selects, found by their
    # option contents so we don't depend on Wicket ids or label order.
    country_sel = _find_select_with_option(scope, [selectors.SIGNUP_DEFAULT_COUNTRY, "united states", "usa"])
    if country_sel is not None:
        _select_option_tolerant(country_sel, [profile.country, selectors.SIGNUP_DEFAULT_COUNTRY, "USA"])
    if profile.state:
        state_sel = _find_select_with_option(
            scope, ["alabama", "california", "new york", "texas", "wyoming", "florida"]
        )
        if state_sel is not None:
            if not _select_option_tolerant(state_sel, state_variants(profile.state)):
                say(f"Could not match state '{profile.state}' in the dropdown - left as-is.")

    # Password + confirm (first two visible password inputs in the form).
    pwd_inputs = [e for e in scope.find_elements(By.CSS_SELECTOR, "input[type='password']") if _is_visible(e)]
    if not pwd_inputs:
        raise SignupError("Could not find the password field on the sign-up form.")
    _fill_element(driver, pwd_inputs[0], profile.password)
    if len(pwd_inputs) >= 2:
        _fill_element(driver, pwd_inputs[1], profile.password)
    else:
        confirm = _find_input_by_placeholder(scope, selectors.SIGNUP_CONFIRM_PLACEHOLDERS)
        if confirm is not None:
            _fill_element(driver, confirm, profile.password)

    # "I am 18 years or older" eligibility radio (required to submit).
    say("Confirming 18+...")
    if not _check_age_radio(driver, selectors.SIGNUP_AGE_OK_LABEL_CONTAINS):
        say("Warning: could not find the '18 or older' option - check SIGNUP_AGE_OK_LABEL_CONTAINS.")


def _inject_assist_overlay(
    driver,
    title: str = "RapidMoto sign-up",
    instructions: str = "1) Click <b>Submit</b> and clear the captcha.<br>2) Once the account is created, click below.",
    ok_label: str = "Account created",
) -> None:
    """Inject a small in-page panel with an OK / Skip button.

    The OK button sets ``window.__rmSignupResult='confirmed'`` and Skip sets
    ``'skipped'``; Python polls that value. Re-injectable after page reloads.
    """
    payload = json.dumps({"title": title, "instructions": instructions, "ok": ok_label})
    js = r"""
    (function(){
      if (document.getElementById('__rm_assist')) return;
      var cfg = %s;
      var d = document.createElement('div');
      d.id = '__rm_assist';
      d.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;'
        + 'background:#14161f;color:#f2f4f8;padding:14px 16px;border:2px solid #2f6bff;'
        + 'border-radius:12px;font-family:Segoe UI,Arial,sans-serif;'
        + 'box-shadow:0 8px 30px rgba(0,0,0,.55);max-width:260px';
      d.innerHTML =
        '<div style="font-weight:700;margin-bottom:6px">' + cfg.title + '</div>'
        + '<div style="font-size:12px;line-height:1.45;margin-bottom:10px;color:#9aa3b2">'
        + cfg.instructions + '</div>'
        + '<button id="__rm_ok" style="background:#22c55e;color:#fff;border:0;'
        + 'padding:9px 12px;border-radius:7px;cursor:pointer;font-weight:700;margin-right:6px">'
        + cfg.ok + ' \u2713</button>'
        + '<button id="__rm_skip" style="background:#ef4444;color:#fff;border:0;'
        + 'padding:9px 12px;border-radius:7px;cursor:pointer;font-weight:700">Skip</button>';
      document.body.appendChild(d);
      if (typeof window.__rmSignupResult === 'undefined') window.__rmSignupResult = '';
      document.getElementById('__rm_ok').onclick = function(){
        window.__rmSignupResult = 'confirmed';
        d.innerHTML = '<div style="font-weight:700">Saving &amp; closing...</div>';
      };
      document.getElementById('__rm_skip').onclick = function(){
        window.__rmSignupResult = 'skipped';
        d.innerHTML = '<div style="font-weight:700">Skipped.</div>';
      };
      // Keep our panel above late-appearing site popups (e.g. RM Cash) by
      // re-raising it to the end of <body> so it wins z-index ties.
      window.__rmKeepTop && clearInterval(window.__rmKeepTop);
      window.__rmKeepTop = setInterval(function(){
        var el = document.getElementById('__rm_assist');
        if (el && document.body.lastElementChild !== el) { document.body.appendChild(el); }
      }, 500);
    })();
    """ % payload
    try:
        driver.execute_script(js)
    except Exception:  # noqa: BLE001
        pass


def assist_signup(
    driver,
    profile: SignupProfile,
    status_cb: Optional[StatusCallback] = None,
    timeout: int = 30,
    wait_timeout: int = 600,
) -> None:
    """Fill the form, scroll to Submit, then wait for YOU to finish.

    The registration Submit is a Google reCAPTCHA button, so a human completes
    it. We fill every field, scroll to Submit, and show an in-page panel with an
    "Account created" button. When you click it (after submitting + clearing the
    captcha in the browser), this returns and the caller saves the account.
    Raises :class:`SignupError` if you Skip, close the window, or time out.
    """
    say = status_cb or (lambda _m: None)
    _open_and_fill(driver, profile, say, timeout)

    # Scroll the Submit button into view so it's easy to click.
    btn = _find_submit_button(driver)
    if btn is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
        except Exception:  # noqa: BLE001
            pass

    _inject_assist_overlay(driver)
    say("Filled. In the browser: click Submit, clear the captcha, then click "
        "'Account created' (top-right box).")

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        try:
            if not driver.window_handles:
                raise SignupError("Browser was closed before you confirmed - not saved.")
        except SignupError:
            raise
        except Exception:  # noqa: BLE001
            raise SignupError("Browser was closed before you confirmed - not saved.")

        try:
            result = driver.execute_script("return window.__rmSignupResult || '';")
        except Exception:  # noqa: BLE001
            result = ""
        if result == "confirmed":
            say("You confirmed the account - saving session.")
            persist_login_cookies(driver)  # keep the login after the browser closes
            return
        if result == "skipped":
            raise SignupError("Skipped by you - not saved.")

        # Re-inject the panel if a page reload/navigation removed it.
        try:
            if not driver.execute_script("return !!document.getElementById('__rm_assist');"):
                _inject_assist_overlay(driver)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)

    raise SignupError("Timed out waiting for you to confirm the account - not saved.")


def _inject_login_overlay(driver, email: str, password: str) -> None:
    """Panel that SHOWS the login (copyable) so YOU type/paste it -- a fully
    human login is far less likely to trip the reCAPTCHA than programmatic fill.
    """
    payload = json.dumps({"email": email, "pw": password})
    js = r"""
    (function(){
      var old = document.getElementById('__rm_assist'); if (old) old.remove();
      var cfg = %s;
      var inp = 'flex:1;background:#0b0d12;color:#fff;border:1px solid #262a36;'
        + 'border-radius:6px;padding:5px;font-size:12px;min-width:0';
      var cbtn = 'background:#2f6bff;color:#fff;border:0;border-radius:6px;'
        + 'padding:0 8px;cursor:pointer;font-size:12px';
      var d = document.createElement('div'); d.id = '__rm_assist';
      d.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;'
        + 'background:#14161f;color:#f2f4f8;padding:14px 16px;border:2px solid #2f6bff;'
        + 'border-radius:12px;font-family:Segoe UI,Arial,sans-serif;'
        + 'box-shadow:0 8px 30px rgba(0,0,0,.55);width:280px';
      d.innerHTML =
        '<div style="font-weight:700;margin-bottom:6px">RapidMoto login</div>'
        + '<div style="font-size:12px;color:#9aa3b2;margin-bottom:8px">'
        + 'Paste these into the form, click <b>Log In</b> and clear the captcha. '
        + 'I detect when you are in.</div>'
        + '<div style="font-size:11px;color:#9aa3b2">Email</div>'
        + '<div style="display:flex;gap:4px;margin-bottom:6px">'
        + '<input id="__rm_em" readonly style="'+inp+'">'
        + '<button id="__rm_cem" style="'+cbtn+'">Copy</button></div>'
        + '<div style="font-size:11px;color:#9aa3b2">Password</div>'
        + '<div style="display:flex;gap:4px;margin-bottom:10px">'
        + '<input id="__rm_pw" readonly style="'+inp+'">'
        + '<button id="__rm_cpw" style="'+cbtn+'">Copy</button></div>'
        + '<button id="__rm_ok" style="background:#22c55e;color:#fff;border:0;'
        + 'padding:9px 12px;border-radius:7px;cursor:pointer;font-weight:700;margin-right:6px">'
        + 'Logged in \u2713</button>'
        + '<button id="__rm_skip" style="background:#ef4444;color:#fff;border:0;'
        + 'padding:9px 12px;border-radius:7px;cursor:pointer;font-weight:700">Skip</button>';
      document.body.appendChild(d);
      document.getElementById('__rm_em').value = cfg.email;
      document.getElementById('__rm_pw').value = cfg.pw;
      if (typeof window.__rmSignupResult === 'undefined') window.__rmSignupResult = '';
      function cp(v, b){ try{ navigator.clipboard.writeText(v); b.textContent='Copied'; }catch(e){} }
      document.getElementById('__rm_cem').onclick = function(){ cp(cfg.email, this); };
      document.getElementById('__rm_cpw').onclick = function(){ cp(cfg.pw, this); };
      document.getElementById('__rm_ok').onclick = function(){
        window.__rmSignupResult='confirmed';
        d.innerHTML='<div style="font-weight:700">Saving &amp; closing...</div>';
      };
      document.getElementById('__rm_skip').onclick = function(){
        window.__rmSignupResult='skipped';
        d.innerHTML='<div style="font-weight:700">Skipped.</div>';
      };
      // Keep our panel above late-appearing site popups (e.g. RM Cash) by
      // re-raising it to the end of <body> so it wins z-index ties.
      window.__rmKeepTop && clearInterval(window.__rmKeepTop);
      window.__rmKeepTop = setInterval(function(){
        var el = document.getElementById('__rm_assist');
        if (el && document.body.lastElementChild !== el) { document.body.appendChild(el); }
      }, 500);
    })();
    """ % payload
    try:
        driver.execute_script(js)
    except Exception:  # noqa: BLE001
        pass


def assist_login(
    driver,
    email: str,
    password: str,
    status_cb: Optional[StatusCallback] = None,
    timeout: int = 30,
    wait_timeout: int = 600,
    autofill: bool = True,
) -> None:
    """Assisted login. By default the tool auto-fills your email + password and
    also shows them in a copyable panel; you click Log In + clear the captcha.
    Set ``autofill=False`` to only show the credentials (no typing).

    Success is auto-detected (rider dropdowns become editable) or you click
    'Logged in'. The session persists in the Chrome profile so later pick runs
    reuse it (no re-login).
    """
    say = status_cb or (lambda _m: None)

    driver.get(config.BASE_URL)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    try:
        WebDriverWait(driver, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass
    time.sleep(1.0)

    if is_logged_in(driver):
        say("Already logged in (session reused) - marking valid.")
        return

    say("Opening login form...")
    link = _find_first_visible(driver, [selectors.LOGIN_LINK_CSS])
    if link is not None:
        _click(driver, link)
    else:
        _click_by_text(driver, ["LOG IN", "Log In", "Login", "Sign In"])

    # Give the login modal a moment to appear (don't hard-fail; you can open it).
    try:
        WebDriverWait(driver, 12).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS))
        )
    except TimeoutException:
        pass

    # By default DON'T type the credentials (programmatic typing lowers the
    # captcha score). Only auto-fill if explicitly asked.
    if autofill:
        try:
            _fill_element(driver, driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS), email)
            _fill_element(driver, driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS), password)
            say("Filled email + password.")
        except Exception:  # noqa: BLE001
            say("Could not auto-fill - paste the shown login instead.")

    _inject_login_overlay(driver, email, password)
    say("Paste the shown login, click Log In & clear the captcha; I detect when you're in.")

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        try:
            if not driver.window_handles:
                raise LoginRequired("Browser was closed before login completed.")
        except LoginRequired:
            raise
        except Exception:  # noqa: BLE001
            raise LoginRequired("Browser was closed before login completed.")

        if is_logged_in(driver):
            say("Logged in - saving session.")
            persist_login_cookies(driver)  # keep the login after the browser closes
            return
        try:
            result = driver.execute_script("return window.__rmSignupResult || '';")
        except Exception:  # noqa: BLE001
            result = ""
        if result == "confirmed":
            say("You confirmed login - saving session.")
            persist_login_cookies(driver)
            return
        if result == "skipped":
            raise LoginRequired("Skipped by you.")
        try:
            if not driver.execute_script("return !!document.getElementById('__rm_assist');"):
                _inject_login_overlay(driver, email, password)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)

    raise LoginRequired("Timed out waiting for login.")


def do_signup(
    driver,
    profile: SignupProfile,
    status_cb: Optional[StatusCallback] = None,
    timeout: int = 30,
    post_submit_dwell: float = 4.0,
    submit_attempts: int = 8,
    submit_retry_delay: float = 3.0,
    pre_submit_delay: float = 3.0,
) -> None:
    """Register a brand-new account from a :class:`SignupProfile` (auto submit).

    Note: on sites where Submit is a reCAPTCHA button, use ``assist_signup``
    instead -- a human must clear the captcha.
    """
    say = status_cb or (lambda _m: None)

    _open_and_fill(driver, profile, say, timeout)

    # A short human-like pause BEFORE submitting. The form is filled very fast,
    # and submitting instantly is what trips the site's "verification failed".
    # (This does not slow the fill -- just adds a beat before the first click.)
    if pre_submit_delay > 0:
        time.sleep(pre_submit_delay)

    # Submit -- and RETRY the click several times. The site's registration is
    # flaky and often reports "verification failed" on the first press, then
    # goes through on a later one (this mirrors having to re-click Submit a few
    # times manually). Before each retry we re-tick the 18+ radio (in case the
    # re-render cleared it) and re-click a fresh submit button. We stop early on
    # a real rejection (duplicate email, weak password, etc.) or once confirmed.
    attempts = max(1, submit_attempts)
    say(f"Submitting registration (try 1/{attempts})...")
    if not _click_signup_submit(driver, status_cb=say):
        raise SignupError(
            "Could not find the registration Submit button. Check "
            "SIGNUP_SUBMIT_TEXTS / SIGNUP_SUBMIT_CSS in selectors.py."
        )

    confirmed = False
    last_error = ""
    for attempt in range(1, attempts + 1):
        # Wait for a STRICT success signal (RM Cash popup / logged in).
        say(f"Waiting for confirmation (try {attempt}/{attempts})...")
        if _confirm_signup(driver, timeout=submit_retry_delay + 4, status_cb=say):
            confirmed = True
            say("Confirmed - account created on site.")
            break
        # Inspect any inline error; bail out only on a clearly fatal one.
        err = _find_first_visible(driver, [selectors.SIGNUP_ERROR_CSS])
        etext = " ".join(err.text.split()) if err and (err.text or "").strip() else ""
        if etext:
            last_error = etext
            if _is_fatal_signup_error(etext):
                raise SignupError(f"Registration rejected: {etext[:200]}")
        if attempt < attempts:
            reason = f" (site said: {etext[:60]})" if etext else " (no confirmation yet)"
            say(f"Not confirmed{reason} - re-clicking Submit (try {attempt + 1}/{attempts})...")
            # Re-assert 18+ (the re-render may clear it), then re-click Submit.
            _check_age_radio(driver, selectors.SIGNUP_AGE_OK_LABEL_CONTAINS)
            time.sleep(submit_retry_delay)
            if not _click_signup_submit(driver, status_cb=say):
                say("Warning: could not find the Submit button to re-click.")

    if not confirmed:
        detail = f" Site said: {last_error[:200]}" if last_error else ""
        raise SignupError(
            f"Registration NOT confirmed after {attempts} submit attempts.{detail} "
            f"No account saved. Increase 'Submit attempts' or check the site."
        )

    # Confirmed -- keep the browser open a moment so everything finalizes.
    if post_submit_dwell > 0:
        say(f"Done - holding browser open {post_submit_dwell:g}s...")
        time.sleep(post_submit_dwell)
