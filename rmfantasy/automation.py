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


@dataclass
class PickRequest:
    core_five: list[str]   # 5 EXACT roster names, ordered 1st..5th
    wildcard: str          # EXACT roster name for the wildcard slot


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().casefold()


# --------------------------------------------------------------------------- #
# Driver lifecycle
# --------------------------------------------------------------------------- #
def build_driver(
    profile_dir: str,
    headless: bool = False,
    proxy: Optional[str] = None,
) -> webdriver.Chrome:
    """Create a Chrome driver bound to an isolated profile directory.

    ChromeDriver is resolved via webdriver-manager when available, otherwise
    Selenium 4's built-in Selenium Manager.
    """
    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1200,860")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")
    if headless:
        opts.add_argument("--headless=new")

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
