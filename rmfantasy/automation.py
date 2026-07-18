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

import json
import logging
import time
from contextlib import contextmanager
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
    # Extra anti-detection: hide navigator.webdriver.
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )
    except Exception:  # pragma: no cover - not fatal
        pass
    return driver


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


@contextmanager
def chrome_session(profile_dir: str, headless: bool = False, proxy: Optional[str] = None):
    driver = build_driver(profile_dir, headless=headless, proxy=proxy)
    try:
        yield driver
    finally:
        try:
            driver.quit()
        except Exception:  # pragma: no cover
            pass


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
    return [el for el in driver.find_elements(By.CSS_SELECTOR, css) if el.is_displayed()]


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
    """Map normalized option text -> exact visible text (skipping placeholders)."""
    out: dict[str, str] = {}
    for opt in Select(select_el).options:
        text = (opt.text or "").strip()
        if _norm(text) in selectors.PLACEHOLDER_OPTION_TEXTS:
            continue
        if text:
            out.setdefault(_norm(text), text)
    return out


def get_round_label(driver) -> str:
    """Best-effort read of the current round title (e.g. 'Round #7 - Spring Creek')."""
    for css in ("h1", "h2", ".roundTitle", ".pickHeader"):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            txt = " ".join((el.text or "").split())
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


def _click(driver, el) -> None:
    try:
        el.click()
    except _TRANSIENT:
        driver.execute_script("arguments[0].click();", el)


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

    email_el = driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_EMAIL_CSS)
    pwd_el = driver.find_element(By.CSS_SELECTOR, selectors.LOGIN_PASSWORD_CSS)
    email_el.clear()
    email_el.send_keys(email)
    pwd_el.clear()
    pwd_el.send_keys(password)

    # Submit: prefer an explicit submit button; fall back to button text.
    submit = _find_first_visible(driver, [selectors.LOGIN_SUBMIT_CSS])
    if submit is not None:
        _click(driver, submit)
    elif not _click_by_text(driver, selectors.LOGIN_SUBMIT_TEXTS):
        # Last resort: submit the form via the password field.
        pwd_el.submit()

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


def ensure_logged_in(
    driver,
    email: str,
    password: str,
    login_timeout: int = 30,
    status_cb: Optional[StatusCallback] = None,
) -> None:
    """Guarantee an authenticated session (reuse saved session, else log in)."""
    driver.get(config.BASE_URL)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    if is_logged_in(driver):
        if status_cb:
            status_cb("Already logged in (reused session).")
        return

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
    for el in scope.find_elements(By.CSS_SELECTOR, "input, textarea"):
        if not _is_visible(el):
            continue
        ph = (el.get_attribute("placeholder") or "").casefold()
        if ph and any(s in ph for s in subs):
            return el
    return None


def _fill_element(el, value: str) -> None:
    try:
        el.clear()
    except Exception:  # noqa: BLE001
        pass
    el.send_keys(value)


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
    _fill_element(el, value)
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
    _click(driver, el)


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
    _scroll_and_click(driver, el)
    say(f"Clicked Submit ('{label or el.tag_name}').")
    return True


def _is_fatal_signup_error(text: str) -> bool:
    t = (text or "").casefold()
    return any(k in t for k in selectors.SIGNUP_FATAL_ERROR_CONTAINS)


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
    """Register a brand-new account from a :class:`SignupProfile`.

    Opens the registration modal, fills every field (random identity + shared
    address, country defaulting to the United States), ticks "I am 18 or
    older", and submits. Raises :class:`SignupError` if the form can't be found
    or the registration isn't confirmed. The browser profile is persisted by
    the caller so the freshly-created session stays logged in.
    """
    say = status_cb or (lambda _m: None)

    driver.get(config.BASE_URL)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    # Let the (JS-rendered) header/buttons settle before we hunt for Sign Up.
    time.sleep(1.0)

    # Open the "new player -> SIGN UP" modal (unless it's already showing).
    say("Opening sign-up form...")
    if not _open_signup_modal(driver, timeout=timeout):
        seen = _visible_click_texts(driver)
        hint = (", ".join(seen) if seen else "(no buttons detected)")
        raise SignupError(
            "Could not open the sign-up form. The buttons/links I could see were: "
            f"[{hint}]. Tell me which one opens sign-up (or share this) so the "
            "SIGNUP_OPEN_TEXTS in selectors.py can be set exactly."
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
    _fill_element(pwd_inputs[0], profile.password)
    if len(pwd_inputs) >= 2:
        _fill_element(pwd_inputs[1], profile.password)
    else:
        confirm = _find_input_by_placeholder(scope, selectors.SIGNUP_CONFIRM_PLACEHOLDERS)
        if confirm is not None:
            _fill_element(confirm, profile.password)

    # "I am 18 years or older" eligibility radio (required to submit).
    say("Confirming 18+...")
    if not _check_age_radio(driver, selectors.SIGNUP_AGE_OK_LABEL_CONTAINS):
        say("Warning: could not find the '18 or older' option - check SIGNUP_AGE_OK_LABEL_CONTAINS.")

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
