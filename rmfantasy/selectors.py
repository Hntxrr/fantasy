"""Selectors and site heuristics for rmfantasysmx.com.

IMPORTANT: the site runs Apache Wicket, so element ids (id1, id9, id13, ...)
are auto-generated and change between page loads/deploys. NOTHING here targets
those ids. We use element type, class, visible text, and *content-based*
heuristics (e.g. "a <select> with 10+ options is a rider dropdown").

Verified against the live site + confirmed by the operator:
  * Login: click ``a.loginLink`` to open ``#loginModal``; inside the modal
    there is an email input, a password input, and a submit button. There is
    no interactive CAPTCHA. SUCCESS = the modal closes (becomes invisible).
  * Picks: the page has 6 rider ``<select>`` dropdowns (each with 20+ options,
    riders listed twice: featured + alphabetical). The first 5 are places
    1st-5th; the 6th is the wildcard.
  * Submit: a button whose text is like "Submit Your Picks" (found by text).
"""

from __future__ import annotations

# --- Login -----------------------------------------------------------------
LOGIN_LINK_CSS = "a.loginLink, a[href='#loginModal']"
LOGIN_MODAL_CSS = "#loginModal"
# Find inputs BY TYPE within the modal (never by Wicket id).
LOGIN_EMAIL_CSS = "#loginModal input[type='email']"
LOGIN_PASSWORD_CSS = "#loginModal input[type='password']"
# Submit button inside the modal (type/text based, id-free). Text fallback used too.
LOGIN_SUBMIT_CSS = "#loginModal button[type='submit'], #loginModal button.login, #loginModal .g-recaptcha"
LOGIN_SUBMIT_TEXTS = ["Log In", "Login", "Sign In", "Submit"]
# Optional: an error message element inside the modal on bad credentials.
LOGIN_ERROR_CSS = "#loginModal .error, #loginModal .alert-danger, #loginModal .parsley-errors-list"

# --- Sign up / registration -------------------------------------------------
# The registration form is a modal opened from the "new player -> SIGN UP"
# button. As with login, we NEVER target Wicket ids: fields are located by
# their (stable, human-readable) placeholder text, associated <label> text, or
# input type. Adjust the strings below if the site wording changes.
SIGNUP_OPEN_TEXTS = [
    "SIGN UP", "Sign Up", "Signup", "Sign up", "Register", "Create Account",
    "Are you a new player? SIGN UP", "New player", "new player",
]
# The signup modal, if it has a stable-ish container. We still fall back to
# "any visible form containing a First Name field" when this isn't found.
SIGNUP_MODAL_CSS = "#signupModal, #registerModal, #signUpModal, .signupModal"

# Field placeholders (case-insensitive *contains* match). Order = priority.
SIGNUP_FIRST_NAME_PLACEHOLDERS = ["first name"]
SIGNUP_LAST_NAME_PLACEHOLDERS = ["last name"]
SIGNUP_EMAIL_PLACEHOLDERS = ["email"]
SIGNUP_PHONE_PLACEHOLDERS = ["123", "555", "phone", "(   )"]
SIGNUP_STREET_PLACEHOLDERS = ["street", "address"]
SIGNUP_CITY_PLACEHOLDERS = ["city"]
SIGNUP_POSTAL_PLACEHOLDERS = ["postal", "zip"]
SIGNUP_NICKNAME_PLACEHOLDERS = ["nickname"]
# Password / confirm are matched by type + position (first pwd = password,
# second = confirm) but placeholders help disambiguate.
SIGNUP_PASSWORD_PLACEHOLDERS = ["password"]
SIGNUP_CONFIRM_PLACEHOLDERS = ["confirm"]

# <label> text used to locate the Country / State <select> elements.
SIGNUP_COUNTRY_LABELS = ["country"]
SIGNUP_STATE_LABELS = ["state"]
SIGNUP_DEFAULT_COUNTRY = "United States"

# The "I am 18 years or older" eligibility radio (matched by nearby label text).
SIGNUP_AGE_OK_LABEL_CONTAINS = ["18 years or older", "18 or older", "over 18", "18+"]

# Submit button for the registration form.
SIGNUP_SUBMIT_TEXTS = ["Submit", "Sign Up", "Register", "Create Account"]
SIGNUP_SUBMIT_CSS = (
    "#signupModal button[type='submit'], #registerModal button[type='submit'], "
    "form.registration button[type='submit'], button.signup, button.register"
)
# An error/validation message inside the signup form on failure.
SIGNUP_ERROR_CSS = (
    ".signup .error, .registration .error, .parsley-errors-list, "
    ".alert-danger, .field-error"
)
# Text that (defensively) signals a successful registration.
SIGNUP_SUCCESS_TEXT_CONTAINS = [
    "welcome",
    "account created",
    "registration complete",
    "successfully registered",
    "thanks for signing up",
    "verify your email",
    "confirmation email",
]

# --- Rider dropdown heuristics ---------------------------------------------
# A <select> is treated as a rider dropdown if it has at least this many options.
MIN_RIDER_OPTIONS = 10
# Number of rider dropdowns expected on the pick page (5 places + 1 wildcard).
EXPECTED_RIDER_SELECTS = 6
WILDCARD_SELECT_INDEX = 5  # 0-based: the 6th dropdown is the wildcard
# Placeholder option text to ignore when scraping the roster.
PLACEHOLDER_OPTION_TEXTS = {"choose one.", "choose one", "select a rider", "", "-"}

# --- Submit picks -----------------------------------------------------------
SUBMIT_PICKS_BUTTON_TEXTS = [
    "Submit Your Picks",
    "Submit Picks",
    "Submit My Picks",
    "Save Picks",
    "Submit",
]
SUBMIT_PICKS_BUTTON_CSS = (
    "button.submitPicks, form.riderPicks button[type='submit'], "
    "button.savePicks"
)

# --- Success confirmation ---------------------------------------------------
# Any of these appearing after submit is treated as success. The site shows a
# confirmation timestamp; we also match common success phrasing defensively.
SUBMIT_SUCCESS_TEXT_CONTAINS = [
    "picks have been submitted",
    "picks submitted",
    "successfully submitted",
    "your picks are in",
    "picks saved",
    "last submitted",
    "submitted on",
]
SUBMIT_SUCCESS_CSS = [
    ".alert-success",
    ".submissionSuccess",
    ".picksSubmitted",
    ".pickTimestamp",
    ".submittedTimestamp",
]
