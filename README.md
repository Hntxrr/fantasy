# RapidMoto Fantasy Pick Bot

A standalone desktop app (Python + CustomTkinter) that manages many
[rmfantasysmx.com](https://www.rmfantasysmx.com) accounts and automates
submitting weekly fantasy picks with Selenium. Clean dark UI with the
RapidMoto (RM) branding.

It handles everything itself — account storage, rider-name resolution,
lineup × wildcard assignment math, and concurrent submission — with no external
tooling required.

---

## What it does

- **Bulk account management** — paste `email:password` lines (100–400+ at a
  time) into SQLite. Each account gets an **isolated Chrome profile**
  (`--user-data-dir`) so cookies/sessions never collide. Sessions persist, so
  after the first login the bot detects *"already logged in"* and skips login.
- **Bulk sign-up** — paste a list of emails + your city/state/postal; the bot
  fabricates a random name, phone, nickname, strong password **and street
  address** for each, ticks *"I am 18 or older"*, submits the registration,
  then saves the new `email:password` to `signups.txt` and adds the account to
  **Accounts** (as *not signed in*, so it sits at the bottom until a login/pick
  run confirms it).
- **Fuzzy rider-name resolution** — you type partials/first names/typos; the app
  scrapes the site's rider dropdown once and resolves them:
  `Jett → Jett Lawrence`, `Jordan smith → Jordon Smith`,
  `Valentine → Valentin Guillod`.
- **Auto-assignment math** — pairs every lineup with every wildcard, one pair
  per account, sequentially:
  `20 lineups × 8 wildcards = 160 accounts` → accounts 1–8 get Lineup 1 with
  wildcards 1–8, accounts 9–16 get Lineup 2, and so on.
- **Start at any account** — pick the account the round begins at from a
  searchable dropdown (type to filter by label/email). The plan assigns from
  that account and goes *down* the list, so you can skip accounts already used
  in a previous round — even if that's account #200 out of 400+.
- **Concurrent submission** — 1–15 browsers at once, each: log in → select the
  5 place picks + wildcard → submit → confirm → close. Optional launch stagger
  and round-robin proxies to avoid rate-limiting.
- **History** — every submission (account, lineup, wildcard, result, message,
  timestamp) is logged to SQLite and viewable in-app.

---

## Install & run

Requires **Python 3.10+** and **Google Chrome** installed.

```bash
pip install -r requirements.txt
python main.py
```

> **Tkinter note:** Tkinter ships with the standard Python installers on
> Windows and macOS. On some Linux distros install it separately, e.g.
> `sudo apt install python3-tk` (Debian/Ubuntu) or
> `sudo dnf install python3-tkinter` (Fedora/Amazon Linux).

ChromeDriver is provisioned automatically (via `webdriver-manager`, falling back
to Selenium's built-in Selenium Manager) — no manual driver download.

### Build a standalone Windows `.exe`

On a Windows machine (with Python + Chrome installed), just run:

```bat
build.bat
```

This installs the dependencies + PyInstaller and builds a single-file windowed
app at `dist\RapidMotoPickBot.exe` using `RMFantasyPickBot.spec` (which bundles
the RapidMoto logo and sets the exe icon). Double-click the exe to run — no
Python needed on that machine afterward. (The `.exe` must be built *on* Windows;
it can't be cross-compiled from Linux/macOS.)

---

## Creating accounts (Sign Up tab)

Need fresh accounts? On the **Sign Up** tab:

1. Paste the **emails** you want to register (one per line).
2. Fill the shared **City**, **State** (dropdown) and **Postal code**.
   **Country** defaults to *United States*. The **street address is randomized**
   for each account automatically.
3. Set options (keep **Concurrent browsers** low — 1–2 — with a 6–10s stagger
   from a single IP; **Keep browser open after submit** gives the registration
   time to finish before the window closes), then click **SIGN UP ALL**.

For each email the bot generates a random first/last name, phone, nickname,
strong password and street address, ticks *"I am 18 or older"*, and submits the
form. On success the `email:password` is appended to `signups.txt` (see *Where
data lives*) and the account is added to **Accounts** as *not signed in* (it
sits at the bottom of the list until a login/pick run confirms the session).
Emails that already exist as accounts are skipped; a signup that fails is rolled
back (no orphan account).

> The registration form is located by field **placeholder/label text** (not the
> site's volatile Wicket ids). If the site changes its wording, tweak the
> `SIGNUP_*` entries in `rmfantasy/selectors.py`.

## Weekly workflow

1. **Accounts tab** — paste `email:password` lines → **Import accounts**.
   (Use **Clear ALL accounts** to wipe and re-import.) New here? Use the
   **Sign Up** tab (above) to create accounts first.
2. **This Round tab**
   - Paste **lineups**, one per line, 5 space-separated names
     (`Jett Hunter Haiden Eli Jorge`).
   - Paste **wildcards**, one per line (`Jordan smith`, `Valentine`, …).
   - **Start at account** — choose which account the round begins at from the
     dropdown (type to filter). Assignment goes *down* the list from there, so
     you can skip accounts already used. Leave it on the first account to use
     them all. **First account** resets it to the top.
   - Click **Scrape riders from site** once (opens a browser using your first
     account to read the current rider roster).
   - Click **Resolve & preview** — check the preview for any `NO MATCH` /
     `AMBIGUOUS` flags and fix those names.
   - Click **Lock in assignments** — builds the account → (lineup, wildcard)
     plan (starting at your chosen account) and shows the math
     (assigned / idle / unassigned, plus how many accounts were skipped ahead
     of the start point).
3. **Run Picks tab** — set your options and hit **RUN PICKS**. Watch the live
   per-account status table.
4. **History tab** — review results, including which wildcard each account used.

### Run Picks tab controls

| Control | What it does |
|---------|--------------|
| **Concurrent browsers** (1–15) | How many accounts run at once. |
| **Launch stagger (s)** | Delay between browser launches, to avoid rate-limiting. |
| **Keep browser open after submit (s)** | Brief pause after a confirmed submit before the (visible) browser closes. Default 0.5s; set to 0 to close instantly, or higher if you want a longer look. |
| **Headless** | Run without visible windows (faster; no dwell). |
| **Begin run at** | Dropdown of the locked plan's rows — start the run at this row and skip earlier ones (handy if you already submitted some manually, or want to resume a partial run). Type to filter, or use **Set from selected row** to fill it from whatever row you click. This is a *within-plan* skip; to change which account the plan itself starts at, use **Start at account** on the This Round tab. |
| **Proxies** | Optional `host:port` per line, assigned round-robin across browsers. |
| **RUN PICKS / STOP** | Start / cooperatively cancel the run. |
| **Retry failed** | Re-runs only the accounts whose last result was a failure. |
| **👁 Watch selected** | Opens a real browser for the selected account (also: double-click a row) so you can watch/verify it. Use when a run isn't active. |
| **Reset round** | Clears the round for a fresh week — see below. |

### Avoiding rate limits / blocks (no proxies)

All your traffic comes from one IP, so the main lever is **going slower**. The
**launch stagger** spaces out when each browser *starts* hitting the site, and
**concurrency** caps how many run at once. Together they set how hard you hit
the site: with stagger `S` seconds you start roughly one browser every `S`
seconds (up to `concurrency` at a time).

Suggested settings from a single home IP:

| Situation | Concurrent | Launch stagger |
|-----------|-----------|----------------|
| Default / safe | 2–3 | 5–8 s |
| You already got blocked / timeouts | 1–2 | 10–15 s (and wait ~15 min first) |
| Never had issues, small batch | 4–5 | 3–4 s |

Extra ways to stay under the radar:
- **Run in batches** with **Begin run at** (e.g. do 20–30, pause a few
  minutes, then continue) instead of blasting all at once.
- After the **first** successful run, logins are saved in each Chrome profile,
  so later runs skip login = far fewer requests = much less likely to be
  blocked. Don't delete the profiles.
- A wave of "site can't be reached" / timeout failures is the signal you're
  going too fast — lower concurrency, raise the stagger, wait, then use
  **Retry failed**.
- Headless vs. visible makes no difference to rate limiting (same requests).

### What persists (and Reset round)

Everything about the current round is saved and **survives closing the app**:
lineups, wildcards, the locked plan, the Run Picks table (with each account's
last status), and the submission History. It all stays until you click
**Reset round**, which clears the round and history for a new week. Your
**accounts** and **saved name overrides** are always kept (only *Clear ALL
accounts* removes accounts).

---

## Where your data lives

A single per-user app directory (override with `RMFANTASY_HOME`):

| OS       | Default location |
|----------|------------------|
| Windows  | `%APPDATA%\RMFantasySMX\` |
| macOS    | `~/Library/Application Support/RMFantasySMX/` |
| Linux    | `~/.local/share/rmfantasysmx/` |

```
rmfantasy.db            SQLite: accounts, lineups, roster, submission log, state
signups.txt             email:password lines for accounts created via Sign Up
secret.key              Fernet key (only if OS keyring is unavailable; chmod 600)
chrome_profiles/<slug>/ one isolated Chrome profile per account
logs/app.log            rolling log
```

> On Windows, paste `%APPDATA%\RMFantasySMX` into the File Explorer address bar
> to open this folder. `signups.txt` is a plaintext convenience export of the
> accounts you create on the **Sign Up** tab.

**Credentials:** passwords are encrypted at rest with Fernet; the key is stored
in your OS keyring (Windows Credential Manager / macOS Keychain / Secret
Service) or a locked-down key file as fallback. (You noted plaintext would be
fine — encryption is kept on because it's transparent and adds no friction.)

---

## Site-specific automation notes (rmfantasysmx.com)

The site runs **Apache Wicket**, so element ids (`id1`, `id9`, …) are
auto-generated and unstable. **Nothing targets those ids.** All selectors live
in [`rmfantasy/selectors.py`](rmfantasy/selectors.py) and use type/class/text
and content heuristics:

- **Login** — click `a.loginLink` → `#loginModal`; fill
  `#loginModal input[type='email']` and `#loginModal input[type='password']`;
  submit. **Success = the modal closes** (and rider dropdowns become enabled).
  There is no interactive CAPTCHA. *(The homepage carries an invisible reCAPTCHA
  v3 badge on the login button, but it does not present a challenge.)*
- **Picks** — the pick page has **6 `<select>` dropdowns with 10+ options**;
  they're found by that content heuristic. The first five are places 1st–5th
  (your lineup), the sixth is the **wildcard**. Riders are selected by visible
  name (`select_by_visible_text`), which correctly picks the first of the two
  listings (featured + alphabetical).
- **Submit** — a button found by text (`Submit Your Picks`, etc.).
- **Sign up** — the registration modal is opened by the *new player* **SIGN UP**
  button; fields are located by **placeholder text** (`First Name`, `Email`,
  …), the Country/State `<select>`s by their **option contents** (the one with
  a `United States` option; the one listing US states), and the **18+** radio by
  its label text. Success = logged in / form closed / a welcome message.
- **AJAX-safe** — explicit `WebDriverWait` throughout; a custom `wait_until_any`
  is used instead of the unreliable `EC.or_`.

### Selectors to confirm after your first live run

These couldn't be verified from the logged-out page. Defaults are sensible;
adjust in `selectors.py` if a run reports "couldn't find submit button" or "no
confirmation detected":

- `SUBMIT_PICKS_BUTTON_TEXTS` — exact submit button label.
- `SUBMIT_SUCCESS_TEXT_CONTAINS` / `SUBMIT_SUCCESS_CSS` — how the site confirms
  a successful submission (a timestamp element or success banner).
- `SIGNUP_*` — the registration field placeholders, the open-signup button
  text, the 18+ label text, and the submit label. Verify these on your first
  live sign-up; they're best-effort guesses from the form layout.

---

## Architecture

```
rmfantasy/
  config.py        paths / app-data dir
  crypto.py        Fernet encryption (keyring or file key)
  database.py      SQLite schema
  models.py        dataclasses
  repository.py    all data access (accounts, lineups, roster, log, meta)
  resolver.py      fuzzy rider-name resolution
  assignment.py    cartesian lineup × wildcard → account plan (with start_offset)
  signup.py        random identity generator + signup models + US states
  selectors.py     ALL web selectors (edit here if the site changes)
  automation.py    Selenium: driver, login, scrape, select, submit, sign up
  runner.py        ConcurrentRunner + SignupRunner (thread pool, stagger, proxies)
  rotation.py      optional per-account wildcard rotation (not used by the
                   cartesian workflow; kept for round-to-round cycling)
  ui/app.py        CustomTkinter app (4 tabs) + thread-safe queue bridge
  ui/assets/       RapidMoto logo.png + window/exe icons (icon.png, icon.ico)
main.py            entry point
tools/generate_logo.py  regenerates the RM logo + icons
tests/             pytest suite + Xvfb UI smoke test
```

### Branding / swapping the logo

The header logo and window/exe icon come from `rmfantasy/ui/assets/`. To use
your own artwork, replace `logo.png` (a wide transparent PNG looks best). For
the Windows exe icon, replace `icon.ico`, or drop in a square `icon.png` and run
`python tools/generate_logo.py` to regenerate the `.ico`. If the assets are
missing, the app falls back to a simple "RM" text badge — nothing breaks.

Background Selenium work runs on worker threads that never touch Tk widgets
directly — they push events onto a `queue.Queue` drained by the Tk main loop via
`after(...)`. Each worker uses its own SQLite connection.

---

## Testing

```bash
pytest -q                                   # 39 unit tests (logic, no browser)
PYTHONPATH=. xvfb-run -a python tests/ui_smoke.py   # headless UI build check (Linux)
```

Unit tests cover encryption round-trips, repository CRUD + bulk import, fuzzy
resolution (all the example mappings), and the assignment math.

---

## Responsible use

This automates **your own** accounts on a free fantasy game. Please respect the
site's terms of service and don't hammer it — keep concurrency reasonable and
use the launch stagger (and proxies if needed) to avoid rate-limiting.
```
