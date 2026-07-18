"""CustomTkinter desktop UI for the RMFantasySMX pick bot.

Four tabs:
  * Accounts   - bulk import (email:password), clear all, list, remove/rename.
  * This Round - paste lineups + wildcards, scrape roster, resolve names,
                 preview, and lock in the account->(lineup,wildcard) plan.
  * Run Picks  - concurrency slider, headless/stagger/proxy options, run/stop,
                 and a live per-account progress table.
  * History    - past submissions (which wildcard was used, success/failure).

Threading model
---------------
Selenium work runs on background threads. Those threads NEVER touch Tk widgets
directly; they push events onto a thread-safe ``queue.Queue`` which the Tk main
loop drains via ``after(...)``. Each background thread also uses its own SQLite
connection (connections are not shareable across threads).
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

try:  # Pillow ships with CustomTkinter; guard just in case.
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]

from .. import automation, config
from ..assignment import AssignmentPlan, RoundAssignment, build_plan
from ..repository import Repository
from ..resolver import RiderResolver
from ..runner import (
    ConcurrentRunner,
    RunCallbacks,
    RunResult,
    SigninCallbacks,
    SigninRunner,
    SignupCallbacks,
    SignupRunner,
    VerifyRunner,
)
from ..signup import US_STATE_NAMES, SignupResult

log = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

PAD = 8

# --------------------------------------------------------------------------- #
# RapidMoto brand palette (clean dark UI, blue accent to match the RM logo).
# --------------------------------------------------------------------------- #
BG          = "#0b0d12"   # window background
SURFACE     = "#14161f"   # cards / frames
SURFACE_2   = "#1a1d28"   # nested surfaces, inputs
HOVER       = "#20242f"
BORDER      = "#262a36"
FG          = "#f2f4f8"   # primary text
FG_MUTED    = "#9aa3b2"   # secondary text
FG_FAINT    = "#6b7280"

BRAND       = "#2f6bff"   # RapidMoto blue
BRAND_HOVER = "#1f5aef"
SUCCESS     = "#22c55e"
SUCCESS_HOV = "#1ea34e"
DANGER      = "#ef4444"
DANGER_HOV  = "#dc3838"
WARNING     = "#e0951a"
WARNING_HOV = "#c98614"
NEUTRAL     = "#3a3f4b"
NEUTRAL_HOV = "#474d5b"

# Treeview (ttk) tag colours.
ROW_OK      = "#5ce08a"
ROW_FAIL    = "#ff8a8a"
ROW_BUSY    = "#ffd27f"
ROW_SKIP    = "#7b828e"

# Asset locations (logo + window/exe icon). Missing files degrade gracefully.
ASSETS_DIR    = Path(__file__).resolve().parent / "assets"
LOGO_PATH     = ASSETS_DIR / "logo.png"
ICON_PNG_PATH = ASSETS_DIR / "icon.png"
ICON_ICO_PATH = ASSETS_DIR / "icon.ico"


def _fmt_account_choice(index: int, account) -> str:
    """One dropdown line for an account: '12. label  (email)' (1-based)."""
    return f"{index + 1}. {account.label}  ({account.email})"


def _plan_to_dict(plan: AssignmentPlan) -> dict:
    """Serialize an AssignmentPlan for persistence between app runs."""
    return {
        "lineup_count": plan.lineup_count,
        "wildcard_count": plan.wildcard_count,
        "pairs_needed": plan.pairs_needed,
        "accounts_available": plan.accounts_available,
        "unassigned_pairs": [list(p) for p in plan.unassigned_pairs],
        "idle_accounts": list(plan.idle_accounts),
        "start_offset": plan.start_offset,
        "skipped_before": plan.skipped_before,
        "assignments": [asdict(a) for a in plan.assignments],
    }


def _plan_from_dict(d: dict) -> AssignmentPlan:
    return AssignmentPlan(
        assignments=[RoundAssignment(**a) for a in d.get("assignments", [])],
        lineup_count=d.get("lineup_count", 0),
        wildcard_count=d.get("wildcard_count", 0),
        pairs_needed=d.get("pairs_needed", 0),
        accounts_available=d.get("accounts_available", 0),
        unassigned_pairs=[tuple(p) for p in d.get("unassigned_pairs", [])],
        idle_accounts=list(d.get("idle_accounts", [])),
        start_offset=d.get("start_offset", 0),
        skipped_before=d.get("skipped_before", 0),
    )


class SearchableComboBox(ctk.CTkComboBox):
    """A CTkComboBox whose dropdown filters as you type.

    Handy for picking from hundreds of accounts: start typing a label, email or
    the account number and the list narrows. If nothing matches, the full list
    is kept so you're never stuck. Callers should resolve the *selection* with a
    tolerant match (exact line, else substring) rather than trusting the raw
    text, since free typing is allowed.
    """

    def __init__(self, master, values=None, **kwargs):
        self._all_values: list[str] = list(values or [])
        super().__init__(master, values=self._all_values or [""], **kwargs)
        try:
            self._entry.bind("<KeyRelease>", self._on_key_release)
        except Exception:  # noqa: BLE001
            pass

    def set_values(self, values) -> None:
        """Replace the full backing list (and what the dropdown shows)."""
        self._all_values = list(values)
        self.configure(values=self._all_values or [""])

    def all_values(self) -> list[str]:
        return list(self._all_values)

    def _on_key_release(self, event) -> None:
        # Let navigation / commit keys pass through untouched.
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab", "Left", "Right"):
            return
        typed = self.get().strip().lower()
        if not typed:
            filtered = self._all_values
        else:
            filtered = [v for v in self._all_values if typed in v.lower()]
        # Narrow the dropdown; never leave it empty so the arrow still works.
        self.configure(values=filtered or self._all_values or [""])
        try:
            self._open_dropdown_menu()
            self._entry.focus_set()
            self._entry.icursor("end")
        except Exception:  # noqa: BLE001
            pass


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RapidMoto - Fantasy Pick Bot")
        self.geometry("1160x800")
        self.minsize(980, 660)
        self.configure(fg_color=BG)

        config.ensure_dirs()
        self.repo = Repository()  # main-thread connection

        # Ordered snapshot of all accounts, used by the "Start at account"
        # dropdown on the This Round tab (kept in sync with refresh_accounts).
        self._round_accounts: list = []
        self._logo_image = None  # keep a ref so CTkImage isn't GC'd

        # Shared run state.
        self.resolver: RiderResolver | None = None
        self.plan: AssignmentPlan | None = None
        self.round_label = "Current round"
        self.events: queue.Queue = queue.Queue()
        self.run_thread: threading.Thread | None = None
        self.scrape_thread: threading.Thread | None = None
        self.signup_thread: threading.Thread | None = None
        self.signin_thread: threading.Thread | None = None
        self.assist_thread: threading.Thread | None = None
        self.runner: ConcurrentRunner | None = None
        self.signup_runner: SignupRunner | None = None
        self.signin_runner: SigninRunner | None = None
        self.assist_runner: SigninRunner | None = None
        self._su_row_by_email: dict[str, str] = {}
        self._run_row_by_account: dict[int, str] = {}
        self._run_status: dict[int, tuple] = {}   # account_id -> (status_text, tag)
        self._watch_threads: list = []

        self._style_treeview()
        self._apply_window_icon()
        self._build_header()

        self.tabs = ctk.CTkTabview(
            self, fg_color=SURFACE, segmented_button_fg_color=SURFACE_2,
            segmented_button_selected_color=BRAND,
            segmented_button_selected_hover_color=BRAND_HOVER,
            segmented_button_unselected_color=SURFACE_2,
            segmented_button_unselected_hover_color=HOVER,
            text_color=FG, border_width=0,
        )
        self.tabs.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        self.tabs.add("Accounts")
        self.tabs.add("Sign Up")
        self.tabs.add("This Round")
        self.tabs.add("Run Picks")
        self.tabs.add("History")

        self._build_accounts_tab(self.tabs.tab("Accounts"))
        self._build_signup_tab(self.tabs.tab("Sign Up"))
        self._build_round_tab(self.tabs.tab("This Round"))
        self._build_run_tab(self.tabs.tab("Run Picks"))
        self._build_history_tab(self.tabs.tab("History"))

        self._load_persisted()
        self.refresh_accounts()
        self.refresh_history()

        # Start the UI event pump.
        self._drain_after_id = self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================== #
    # Header / branding
    # ================================================================== #
    def _apply_window_icon(self) -> None:
        """Set the OS window icon from bundled assets (best effort)."""
        try:
            if sys.platform.startswith("win") and ICON_ICO_PATH.exists():
                self.iconbitmap(str(ICON_ICO_PATH))
            elif ICON_PNG_PATH.exists():
                self._win_icon = tk.PhotoImage(file=str(ICON_PNG_PATH))
                self.iconphoto(True, self._win_icon)
            elif LOGO_PATH.exists():
                self._win_icon = tk.PhotoImage(file=str(LOGO_PATH))
                self.iconphoto(True, self._win_icon)
        except Exception:  # noqa: BLE001
            pass

    def _build_header(self) -> None:
        """Top branding bar: RM logo + RapidMoto wordmark + tagline."""
        bar = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=14, height=76)
        bar.pack(fill="x", padx=PAD, pady=(PAD, PAD))
        bar.pack_propagate(False)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=(16, 0), pady=10)

        # Logo image (falls back to a text badge if the asset is missing).
        logo_added = False
        if Image is not None and LOGO_PATH.exists():
            try:
                img = Image.open(LOGO_PATH)
                w, h = img.size
                target_h = 44
                target_w = max(1, int(w * (target_h / h)))
                self._logo_image = ctk.CTkImage(
                    light_image=img, dark_image=img, size=(target_w, target_h)
                )
                ctk.CTkLabel(left, image=self._logo_image, text="").pack(side="left")
                logo_added = True
            except Exception:  # noqa: BLE001
                logo_added = False
        if not logo_added:
            badge = ctk.CTkLabel(
                left, text="RM", fg_color=BRAND, corner_radius=8,
                width=54, height=44, text_color="#ffffff",
                font=ctk.CTkFont(size=22, weight="bold"),
            )
            badge.pack(side="left")

        text_wrap = ctk.CTkFrame(bar, fg_color="transparent")
        text_wrap.pack(side="left", padx=14, pady=10)
        ctk.CTkLabel(
            text_wrap, text="RapidMoto",
            font=ctk.CTkFont(size=24, weight="bold"), text_color=FG,
        ).pack(anchor="w")
        ctk.CTkLabel(
            text_wrap, text="Fantasy SMX  \u2022  automated weekly picks",
            font=ctk.CTkFont(size=12), text_color=FG_MUTED,
        ).pack(anchor="w")

        self.header_status = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=12), text_color=BRAND,
        )
        self.header_status.pack(side="right", padx=18)

    # ================================================================== #
    # Styling
    # ================================================================== #
    def _style_treeview(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("default")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            background=SURFACE_2, foreground=FG, fieldbackground=SURFACE_2,
            rowheight=26, borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE, foreground=FG_MUTED,
            relief="flat", borderwidth=0,
        )
        style.map("Treeview.Heading", background=[("active", HOVER)])
        style.map("Treeview", background=[("selected", BRAND)],
                  foreground=[("selected", "#ffffff")])

    # ================================================================== #
    # Accounts tab
    # ================================================================== #
    def _build_accounts_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        header = ctk.CTkLabel(
            tab, text="Bulk import accounts  (one per line:  email:password)",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        header.grid(row=0, column=0, sticky="w", padx=PAD, pady=(PAD, 0))

        self.import_box = ctk.CTkTextbox(tab, height=200)
        self.import_box.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)
        self.import_box.insert("1.0", "# paste email:password lines here\n")

        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=PAD)
        ctk.CTkButton(btns, text="Import accounts", command=self.on_import).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Clear ALL accounts", fg_color="#8a2c2c",
                      hover_color="#a13636", command=self.on_clear_all).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Refresh", command=self.refresh_accounts).pack(side="left", padx=4)

        self.accounts_status = ctk.CTkLabel(tab, text="", anchor="w", text_color="#8fd18f")
        self.accounts_status.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(4, PAD))

        # Right: account list.
        right = ctk.CTkFrame(tab)
        right.grid(row=0, column=1, rowspan=4, sticky="nsew", padx=PAD, pady=PAD)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.accounts_count_lbl = ctk.CTkLabel(
            right, text="Accounts: 0", font=ctk.CTkFont(size=13, weight="bold")
        )
        self.accounts_count_lbl.grid(row=0, column=0, sticky="w", padx=6, pady=6)

        tree_wrap = tk.Frame(right, bg="#242424")
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.accounts_tree = ttk.Treeview(
            tree_wrap, columns=("email", "session"), show="tree headings", height=16
        )
        self.accounts_tree.heading("#0", text="Label")
        self.accounts_tree.heading("email", text="Email")
        self.accounts_tree.heading("session", text="Session")
        self.accounts_tree.column("#0", width=140)
        self.accounts_tree.column("email", width=240)
        self.accounts_tree.column("session", width=90, anchor="center")
        # Logged-in accounts are tinted green; ones needing a login are muted.
        self.accounts_tree.tag_configure("valid", foreground=ROW_OK)
        self.accounts_tree.tag_configure("invalid", foreground=FG_MUTED)
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.accounts_tree.yview)
        self.accounts_tree.configure(yscrollcommand=vsb.set)
        self.accounts_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        acc_btns = ctk.CTkFrame(right, fg_color="transparent")
        acc_btns.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        ctk.CTkButton(acc_btns, text="Rename / change password",
                      command=self.on_edit_account).pack(side="left", padx=4)
        ctk.CTkButton(acc_btns, text="Remove selected", fg_color=DANGER,
                      hover_color=DANGER_HOV, command=self.on_remove_account).pack(side="left", padx=4)

        login_btns = ctk.CTkFrame(right, fg_color="transparent")
        login_btns.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 4))
        self.assist_login_btn = ctk.CTkButton(
            login_btns, text="Log in selected (auto-fill)", fg_color=BRAND,
            hover_color=BRAND_HOVER, command=self.on_assist_login,
        )
        self.assist_login_btn.pack(side="left", padx=4)
        self.assist_stop_btn = ctk.CTkButton(
            login_btns, text="Stop", fg_color=DANGER, hover_color=DANGER_HOV,
            width=70, state="disabled", command=self.on_assist_stop,
        )
        self.assist_stop_btn.pack(side="left", padx=4)
        ctk.CTkLabel(login_btns, text="Concurrent:").pack(side="left", padx=(10, 2))
        self.login_conc_entry = ctk.CTkEntry(login_btns, width=48)
        self.login_conc_entry.insert(0, "5")
        self.login_conc_entry.pack(side="left", padx=2)

        signin_btns = ctk.CTkFrame(right, fg_color="transparent")
        signin_btns.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 6))
        self.open_chrome_btn = ctk.CTkButton(
            signin_btns, text="Open selected in Chrome", fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOV, command=self.on_open_chrome_login,
        )
        self.open_chrome_btn.pack(side="left", padx=4)
        self.refresh_logins_btn = ctk.CTkButton(
            signin_btns, text="Refresh login status", fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOV, command=self.on_refresh_logins,
        )
        self.refresh_logins_btn.pack(side="left", padx=4)
        self.refresh_stop_btn = ctk.CTkButton(
            signin_btns, text="Stop", fg_color=DANGER, hover_color=DANGER_HOV,
            width=70, state="disabled", command=self.on_refresh_stop,
        )
        self.refresh_stop_btn.pack(side="left", padx=4)

        signin_btns2 = ctk.CTkFrame(right, fg_color="transparent")
        signin_btns2.grid(row=6, column=0, sticky="ew", padx=6, pady=(0, 6))
        ctk.CTkButton(
            signin_btns2, text="Mark selected as logged in", fg_color=SUCCESS,
            hover_color=SUCCESS_HOV, command=self.on_mark_logged_in,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            signin_btns2, text="Mark selected as NOT logged in", fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOV, command=self.on_mark_logged_out,
        ).pack(side="left", padx=4)
        ctk.CTkLabel(
            right,
            text=("Tip: 'Log in selected (auto-fill)' fills each login for you - clear the "
                  "captcha + confirm. Or 'Open selected in Chrome' to do it in a normal "
                  "window. Then picks reuse the saved session."),
            text_color=FG_MUTED, wraplength=460, justify="left", font=ctk.CTkFont(size=11),
        ).grid(row=5, column=0, sticky="ew", padx=6, pady=(0, 4))

    def on_import(self) -> None:
        text = self.import_box.get("1.0", "end")
        result = self.repo.bulk_import_accounts(text)
        self.refresh_accounts()
        msg = result.summary()
        self.accounts_status.configure(text=msg)
        if result.errors:
            messagebox.showwarning(
                "Import finished with issues",
                msg + "\n\nFirst issues:\n" + "\n".join(result.errors[:10]),
            )

    def on_clear_all(self) -> None:
        n = self.repo.count_accounts()
        if n == 0:
            self.accounts_status.configure(text="No accounts to clear.")
            return
        if messagebox.askyesno("Clear ALL accounts",
                               f"Delete all {n} accounts?\n(Chrome profile folders are kept on disk.)"):
            removed = self.repo.clear_all_accounts()
            self.refresh_accounts()
            self.accounts_status.configure(text=f"Cleared {removed} accounts.")

    def refresh_accounts(self) -> None:
        for iid in self.accounts_tree.get_children():
            self.accounts_tree.delete(iid)
        accounts = self.repo.list_accounts()
        valid_n = 0
        for acc in accounts:
            self.accounts_tree.insert(
                "", "end", iid=str(acc.id), text=acc.label,
                values=(acc.email, "valid" if acc.session_valid else "-"),
                tags=("valid",) if acc.session_valid else ("invalid",),
            )
            if acc.session_valid:
                valid_n += 1
        # Valid (logged-in) accounts are listed first, starting at position 1;
        # the rest sit below so you can spot and fix the ones not logged in.
        self.accounts_count_lbl.configure(
            text=f"Accounts: {len(accounts)}   ({valid_n} logged in)"
        )
        # Keep the "Start at account" dropdown (This Round tab) in sync.
        self._refresh_start_at_choices()

    def _selected_account_id(self) -> int | None:
        sel = self.accounts_tree.selection()
        return int(sel[0]) if sel else None

    def on_remove_account(self) -> None:
        acc_id = self._selected_account_id()
        if acc_id is None:
            messagebox.showinfo("No selection", "Select an account in the list first.")
            return
        acc = self.repo.get_account(acc_id, include_password=False)
        if messagebox.askyesno("Remove account", f"Remove '{acc.label}' ({acc.email})?"):
            self.repo.delete_account(acc_id)
            self.refresh_accounts()

    def on_edit_account(self) -> None:
        acc_id = self._selected_account_id()
        if acc_id is None:
            messagebox.showinfo("No selection", "Select an account first.")
            return
        acc = self.repo.get_account(acc_id, include_password=False)
        EditAccountDialog(self, acc_id, acc.label, acc.email)

    # --- Log in via your real Chrome, then verify ------------------------ #
    def _selected_account_ids(self) -> list[int]:
        return [int(iid) for iid in self.accounts_tree.selection() if iid.isdigit()]

    def on_open_chrome_login(self) -> None:
        """Open a normal Chrome window per selected account for manual login."""
        ids = self._selected_account_ids()
        if not ids:
            messagebox.showinfo(
                "No selection",
                "Select the accounts to log in (Ctrl-click or Shift-click for several).",
            )
            return
        if automation.find_chrome_path() is None:
            messagebox.showerror(
                "Chrome not found",
                "Couldn't find Google Chrome. Install it (or tell me the path to "
                "chrome.exe) and try again.",
            )
            return
        if len(ids) > 15 and not messagebox.askyesno(
            "Open many windows?",
            f"This opens {len(ids)} Chrome windows at once - that's a lot to log "
            f"into by hand. Continue?",
        ):
            return
        # Gather credentials on the main thread (DB access), then hand off.
        targets = []
        for i in ids:
            acc = self.repo.get_account(i, include_password=True)
            if acc is not None:
                targets.append((acc.email, acc.password, acc.profile_dir))
        self.accounts_status.configure(
            text=f"Opening {len(targets)} Chrome window(s) - log in, then click 'Refresh login status'."
        )
        threading.Thread(target=self._open_chrome_worker, args=(targets,), daemon=True).start()

    def _open_chrome_worker(self, targets) -> None:
        for email, password, profile_dir in targets:
            try:
                landing = automation.build_login_landing(email, password)
                automation.open_profile_browser(profile_dir, landing)
            except Exception as exc:  # noqa: BLE001
                self.events.put(("si_error", str(exc)))
                return
            time.sleep(1.5)  # small gap so the windows open cleanly
        self.events.put(("chrome_opened", len(targets)))

    def on_refresh_logins(self) -> None:
        """Check which SELECTED accounts have a live session and mark them valid.

        With nothing selected, offers to check every account.
        """
        if self.signin_thread and self.signin_thread.is_alive():
            return
        ids = self._selected_account_ids()
        if not ids:
            total = self.repo.count_accounts()
            if total == 0:
                return
            if not messagebox.askyesno(
                "No selection",
                f"No accounts are selected. Check ALL {total} accounts?\n\n"
                f"(Select specific accounts first to check only those.)",
            ):
                return
            ids = [a.id for a in self.repo.list_accounts()]
        if not ids:
            return
        self.refresh_logins_btn.configure(state="disabled", text="Checking...")
        self.open_chrome_btn.configure(state="disabled")
        self.refresh_stop_btn.configure(state="normal", text="Stop")
        self.accounts_status.configure(text=f"Quick login check (reading cookies)... 0/{len(ids)}")
        # Fast mode reads each profile's saved cookies directly -- no browser
        # launch, so 400+ accounts check in seconds instead of forever.
        self.signin_runner = VerifyRunner(ids, concurrency=8, fast=True)
        self.signin_thread = threading.Thread(target=self._refresh_logins_worker, args=(ids,), daemon=True)
        self.signin_thread.start()

    def _refresh_logins_worker(self, ids) -> None:
        cb = SigninCallbacks(
            on_result=lambda aid, ok, m: self.events.put(("si_result", aid, ok, m)),
            on_progress=lambda d, t: self.events.put(("si_progress", d, t)),
        )
        try:
            self.signin_runner.run(cb)
            self.events.put(("si_done",))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("si_error", str(exc)))

    def on_refresh_stop(self) -> None:
        if self.signin_runner:
            self.signin_runner.cancel()
            self.refresh_stop_btn.configure(state="disabled", text="Stopping...")

    def on_assist_login(self) -> None:
        """Auto-fill login for selected accounts; you clear the captcha + confirm."""
        if self.assist_thread and self.assist_thread.is_alive():
            return
        ids = self._selected_account_ids()
        if not ids:
            messagebox.showinfo("No selection", "Select the accounts to log in.")
            return
        try:
            conc = int(self.login_conc_entry.get() or "5")
        except Exception:  # noqa: BLE001
            conc = 5
        conc = max(1, min(30, conc))
        try:
            stagger = float(self.su_stagger_entry.get() or "3")
        except Exception:  # noqa: BLE001
            stagger = 3.0
        proxies = [ln.strip() for ln in self.su_proxy_box.get("1.0", "end").splitlines() if ln.strip()]
        self.assist_runner = SigninRunner(
            ids, concurrency=conc, launch_stagger=stagger, proxies=proxies,
        )
        self.assist_login_btn.configure(state="disabled", text="Logging in...")
        self.assist_stop_btn.configure(state="normal", text="Stop")
        self.accounts_status.configure(
            text=f"Auto-fill login for {len(ids)} account(s) - clear the captcha + confirm in each window."
        )
        self.assist_thread = threading.Thread(target=self._assist_worker, args=(ids,), daemon=True)
        self.assist_thread.start()

    def _assist_worker(self, ids: list[int]) -> None:
        cb = SigninCallbacks(
            on_task_start=lambda aid: self.events.put(("sl_status", aid, "Opening browser...")),
            on_status=lambda aid, m: self.events.put(("sl_status", aid, m)),
            on_result=lambda aid, ok, m: self.events.put(("sl_result", aid, ok, m)),
            on_progress=lambda d, t: self.events.put(("sl_progress", d, t)),
        )
        try:
            self.assist_runner.run(cb)
            self.events.put(("sl_done",))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("sl_error", str(exc)))

    def on_assist_stop(self) -> None:
        if self.assist_runner:
            self.assist_runner.cancel()
            self.assist_stop_btn.configure(state="disabled", text="Stopping...")

    def on_mark_logged_in(self) -> None:
        """Instantly flag selected accounts as logged in (no check)."""
        ids = self._selected_account_ids()
        if not ids:
            messagebox.showinfo("No selection", "Select the accounts to mark as logged in.")
            return
        for aid in ids:
            self.repo.set_session_valid(aid, True)
        self.refresh_accounts()
        self.accounts_status.configure(text=f"Marked {len(ids)} account(s) as logged in.")

    def on_mark_logged_out(self) -> None:
        """Instantly flag selected accounts as NOT logged in (no check)."""
        ids = self._selected_account_ids()
        if not ids:
            messagebox.showinfo("No selection", "Select the accounts to mark as not logged in.")
            return
        for aid in ids:
            self.repo.set_session_valid(aid, False)
        self.refresh_accounts()
        self.accounts_status.configure(text=f"Marked {len(ids)} account(s) as not logged in.")

    # ================================================================== #
    # Sign Up tab
    # ================================================================== #
    def _build_signup_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=3)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(1, weight=1)

        # --- Left: emails to register ------------------------------------ #
        ctk.CTkLabel(
            tab, text="Emails to sign up  (one per line)",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=PAD, pady=(PAD, 0))
        self.signup_emails_box = ctk.CTkTextbox(tab, fg_color=SURFACE_2)
        self.signup_emails_box.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)
        self.signup_emails_box.insert("1.0", "# one email per line\n")

        # --- Right: shared mailing address ------------------------------- #
        addr = ctk.CTkFrame(tab, fg_color=SURFACE_2, corner_radius=12)
        addr.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=PAD, pady=(PAD, PAD))
        addr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            addr, text="Mailing address  (shared by all)",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=FG,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 6))

        ctk.CTkLabel(addr, text="Street:", text_color=FG_MUTED).grid(row=1, column=0, sticky="w", padx=(14, 6), pady=5)
        ctk.CTkLabel(
            addr, text="\U0001F3B2 randomized each account", text_color=BRAND, anchor="w",
        ).grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=5)

        ctk.CTkLabel(addr, text="City:", text_color=FG_MUTED).grid(row=2, column=0, sticky="w", padx=(14, 6), pady=5)
        self.su_city = ctk.CTkEntry(addr, fg_color=SURFACE, border_color=BORDER)
        self.su_city.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=5)

        ctk.CTkLabel(addr, text="State:", text_color=FG_MUTED).grid(row=3, column=0, sticky="w", padx=(14, 6), pady=5)
        self.su_state = SearchableComboBox(
            addr, values=list(US_STATE_NAMES),
            fg_color=SURFACE, button_color=BRAND, button_hover_color=BRAND_HOVER,
            border_color=BORDER, dropdown_fg_color=SURFACE,
            dropdown_hover_color=HOVER, dropdown_text_color=FG,
        )
        self.su_state.set("")
        self.su_state.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=5)

        ctk.CTkLabel(addr, text="Postal code:", text_color=FG_MUTED).grid(row=4, column=0, sticky="w", padx=(14, 6), pady=5)
        self.su_postal = ctk.CTkEntry(addr, fg_color=SURFACE, border_color=BORDER)
        self.su_postal.grid(row=4, column=1, sticky="ew", padx=(0, 14), pady=5)

        ctk.CTkLabel(addr, text="Country:", text_color=FG_MUTED).grid(row=5, column=0, sticky="w", padx=(14, 6), pady=5)
        self.su_country = ctk.CTkEntry(addr, fg_color=SURFACE, border_color=BORDER)
        self.su_country.insert(0, "United States")
        self.su_country.grid(row=5, column=1, sticky="ew", padx=(0, 14), pady=5)

        ctk.CTkLabel(
            addr,
            text=("First/last name, phone, nickname and password are generated "
                  "randomly for each email, and \u201cI am 18 or older\u201d is "
                  "ticked automatically."),
            text_color=FG_MUTED, wraplength=300, justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 12))

        # --- Options ----------------------------------------------------- #
        opts = ctk.CTkFrame(tab, fg_color=SURFACE)
        opts.grid(row=2, column=0, columnspan=2, sticky="ew", padx=PAD, pady=(0, PAD))
        opts.grid_columnconfigure(9, weight=1)

        ctk.CTkLabel(opts, text="Concurrent browsers:").grid(row=0, column=0, padx=(8, 4), pady=8)
        self.su_conc_value = ctk.CTkLabel(opts, text="1", width=28)
        self.su_conc_slider = ctk.CTkSlider(
            opts, from_=1, to=10, number_of_steps=9, width=140,
            command=lambda v: self.su_conc_value.configure(text=str(int(v))),
        )
        self.su_conc_slider.set(1)
        self.su_conc_slider.grid(row=0, column=1, padx=4)
        self.su_conc_value.grid(row=0, column=2, padx=(0, 12))

        ctk.CTkLabel(opts, text="Launch stagger (s):").grid(row=0, column=3, padx=(8, 4))
        self.su_stagger_entry = ctk.CTkEntry(opts, width=54)
        self.su_stagger_entry.insert(0, "6")
        self.su_stagger_entry.grid(row=0, column=4, padx=4)

        ctk.CTkLabel(opts, text="Keep open after submit (s):").grid(row=0, column=5, padx=(8, 4))
        self.su_keepopen_entry = ctk.CTkEntry(opts, width=48)
        self.su_keepopen_entry.insert(0, "5")
        self.su_keepopen_entry.grid(row=0, column=6, padx=4)

        ctk.CTkLabel(opts, text="Submit attempts:").grid(row=0, column=7, padx=(8, 4))
        self.su_attempts_entry = ctk.CTkEntry(opts, width=48)
        self.su_attempts_entry.insert(0, "8")
        self.su_attempts_entry.grid(row=0, column=8, padx=4)

        self.su_assist_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opts, text="Assist mode (I click Submit + captcha)",
            variable=self.su_assist_var,
        ).grid(row=0, column=9, padx=12, sticky="e")

        self.su_headless_var = ctk.BooleanVar(value=False)

        ctk.CTkLabel(opts, text="Proxies (one per line: host:port OR host:port:user:pass; round-robin):"
                     ).grid(row=1, column=0, columnspan=8, sticky="w", padx=8, pady=(4, 0))
        self.su_proxy_box = ctk.CTkTextbox(opts, height=44, fg_color=SURFACE_2)
        self.su_proxy_box.grid(row=2, column=0, columnspan=8, sticky="ew", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            opts,
            text=("Assist mode (recommended): the form is auto-filled and scrolled to "
                  "Submit; YOU click Submit and clear the site's captcha, then click "
                  "'Account created' in the little box in the browser - it saves the "
                  "account and closes. Keep concurrency low (1-2) so you can handle "
                  "each browser."),
            text_color=FG_MUTED, wraplength=1040, justify="left",
        ).grid(row=3, column=0, columnspan=8, sticky="w", padx=8, pady=(0, 8))

        # --- Actions ----------------------------------------------------- #
        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", padx=PAD)
        self.signup_btn = ctk.CTkButton(
            actions, text="SIGN UP ALL", fg_color=SUCCESS, hover_color=SUCCESS_HOV,
            height=40, width=140, font=ctk.CTkFont(size=15, weight="bold"),
            command=self.on_signup_run,
        )
        self.signup_btn.pack(side="left", padx=4)
        self.signup_stop_btn = ctk.CTkButton(
            actions, text="STOP", fg_color=DANGER, hover_color=DANGER_HOV,
            height=40, width=80, state="disabled", command=self.on_signup_stop,
        )
        self.signup_stop_btn.pack(side="left", padx=4)
        self.test_proxy_btn = ctk.CTkButton(
            actions, text="Test proxy", fg_color=NEUTRAL, hover_color=NEUTRAL_HOV,
            height=40, width=100, command=self.on_test_proxy,
        )
        self.test_proxy_btn.pack(side="left", padx=4)
        self.debug_form_btn = ctk.CTkButton(
            actions, text="Debug form", fg_color=NEUTRAL, hover_color=NEUTRAL_HOV,
            height=40, width=100, command=self.on_debug_form,
        )
        self.debug_form_btn.pack(side="left", padx=4)
        self.signup_progress = ctk.CTkProgressBar(actions, width=220)
        self.signup_progress.set(0)
        self.signup_progress.pack(side="left", padx=12)
        self.signup_progress_lbl = ctk.CTkLabel(actions, text="0 / 0")
        self.signup_progress_lbl.pack(side="left")

        self.signup_status_lbl = ctk.CTkLabel(
            tab, text=f"Saved logins are written to: {config.SIGNUPS_PATH}",
            anchor="w", text_color=FG_MUTED,
        )
        self.signup_status_lbl.grid(row=4, column=0, columnspan=2, sticky="ew", padx=PAD, pady=(6, 0))

        # --- Results table ----------------------------------------------- #
        wrap = tk.Frame(tab, bg=SURFACE_2)
        wrap.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=PAD, pady=PAD)
        tab.grid_rowconfigure(5, weight=1)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        cols = ("email", "status")
        self.signup_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        self.signup_tree.heading("email", text="Email")
        self.signup_tree.heading("status", text="Status")
        self.signup_tree.column("email", width=300, anchor="w")
        self.signup_tree.column("status", width=560, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.signup_tree.yview)
        self.signup_tree.configure(yscrollcommand=vsb.set)
        self.signup_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.signup_tree.tag_configure("ok", foreground=ROW_OK)
        self.signup_tree.tag_configure("fail", foreground=ROW_FAIL)
        self.signup_tree.tag_configure("busy", foreground=ROW_BUSY)
        self.signup_tree.tag_configure("skip", foreground=ROW_SKIP)

    def _parse_signup_emails(self) -> list[str]:
        """Emails from the box: strip, drop blanks/#comments, dedupe, need '@'."""
        out, seen = [], set()
        for raw in self.signup_emails_box.get("1.0", "end").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key = line.casefold()
            if "@" not in line or key in seen:
                continue
            seen.add(key)
            out.append(line)
        return out

    def _su_update_row(self, email: str, status: str, tag: str) -> None:
        iid = self._su_row_by_email.get(email)
        if iid and self.signup_tree.exists(iid):
            self.signup_tree.set(iid, "status", status)
            self.signup_tree.item(iid, tags=(tag,) if tag else ())

    def on_signup_run(self) -> None:
        if self.signup_thread and self.signup_thread.is_alive():
            return
        emails = self._parse_signup_emails()
        if not emails:
            messagebox.showwarning("No emails", "Paste at least one email (one per line).")
            return
        state = self.su_state.get().strip()
        if not state and not messagebox.askyesno(
            "No state selected",
            "No state is selected. The site usually requires one and the "
            "signups may fail.\n\nContinue anyway?",
        ):
            return

        # Populate the results table.
        for iid in self.signup_tree.get_children():
            self.signup_tree.delete(iid)
        self._su_row_by_email.clear()
        for email in emails:
            iid = f"su::{email}"
            self._su_row_by_email[email] = iid
            self.signup_tree.insert("", "end", iid=iid, values=(email, "Pending"), tags=())

        try:
            stagger = float(self.su_stagger_entry.get() or "6")
        except ValueError:
            stagger = 6.0
        try:
            keep_open = float(self.su_keepopen_entry.get() or "5")
        except ValueError:
            keep_open = 5.0
        try:
            attempts = int(self.su_attempts_entry.get() or "8")
        except ValueError:
            attempts = 8
        proxies = [ln.strip() for ln in self.su_proxy_box.get("1.0", "end").splitlines() if ln.strip()]

        self.signup_runner = SignupRunner(
            city=self.su_city.get().strip(),
            state=state,
            postal_code=self.su_postal.get().strip(),
            country=self.su_country.get().strip() or "United States",
            concurrency=int(self.su_conc_slider.get()),
            headless=self.su_headless_var.get(),
            launch_stagger=stagger,
            proxies=proxies,
            post_submit_dwell=keep_open,
            submit_attempts=attempts,
            assist=self.su_assist_var.get(),
        )
        self.signup_progress.set(0)
        self.signup_progress_lbl.configure(text=f"0 / {len(emails)}")
        self._set_signup_running(True)
        self.signup_thread = threading.Thread(
            target=self._signup_worker, args=(emails,), daemon=True
        )
        self.signup_thread.start()

    def _signup_worker(self, emails: list[str]) -> None:
        cb = SignupCallbacks(
            on_task_start=lambda e: self.events.put(("su_task_start", e)),
            on_status=lambda e, m: self.events.put(("su_status", e, m)),
            on_result=lambda r: self.events.put(("su_result", r)),
            on_progress=lambda d, t: self.events.put(("su_progress", d, t)),
        )
        try:
            self.signup_runner.run(emails, cb)
            self.events.put(("su_run_done",))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("su_run_error", str(exc)))

    def on_signup_stop(self) -> None:
        if self.signup_runner:
            self.signup_runner.cancel()
            self.signup_stop_btn.configure(state="disabled", text="Stopping...")

    def on_test_proxy(self) -> None:
        """Open a browser through the first proxy and report its public IP."""
        proxies = [ln.strip() for ln in self.su_proxy_box.get("1.0", "end").splitlines() if ln.strip()]
        proxy = proxies[0] if proxies else None
        if not proxy:
            messagebox.showinfo(
                "No proxy", "Add a proxy line first (host:port or host:port:user:pass).")
            return
        self.test_proxy_btn.configure(state="disabled", text="Testing...")
        self.signup_status_lbl.configure(text=f"Opening browser through proxy {proxy.split(':')[0]}...")
        threading.Thread(target=self._test_proxy_worker, args=(proxy,), daemon=True).start()

    def _test_proxy_worker(self, proxy: str) -> None:
        import tempfile
        prof = tempfile.mkdtemp(prefix="rm_proxytest_")
        try:
            driver = automation.build_driver(prof, headless=False, proxy=proxy)
            try:
                ip = automation.get_public_ip(driver)
            finally:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
            self.events.put(("proxy_ip", ip or "(could not read IP)"))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("proxy_err", str(exc)))

    def on_debug_form(self) -> None:
        """Open the signup modal and dump its buttons/fields to a file."""
        proxies = [ln.strip() for ln in self.su_proxy_box.get("1.0", "end").splitlines() if ln.strip()]
        proxy = proxies[0] if proxies else None
        self.debug_form_btn.configure(state="disabled", text="Dumping...")
        self.signup_status_lbl.configure(text="Opening sign-up form to dump its fields...")
        threading.Thread(target=self._debug_form_worker, args=(proxy,), daemon=True).start()

    def _debug_form_worker(self, proxy) -> None:
        import tempfile
        prof = tempfile.mkdtemp(prefix="rm_debugform_")
        try:
            driver = automation.build_driver(prof, headless=False, proxy=proxy)
            try:
                driver.get(config.BASE_URL)
                automation._open_signup_modal(driver, timeout=30)
                time.sleep(1.0)
                report = automation.dump_signup_form(driver)
            finally:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
            config.ensure_dirs()
            path = config.APP_DIR / "signup_form_debug.txt"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
            self.events.put(("debug_dump", str(path), report))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("debug_err", str(exc)))

    def _set_signup_running(self, running: bool) -> None:
        self.signup_btn.configure(state="disabled" if running else "normal")
        self.signup_stop_btn.configure(state="normal" if running else "disabled", text="STOP")

    def _append_signup_file(self, email: str, password: str) -> None:
        """Append one email:password line to the signups export (main thread)."""
        try:
            config.ensure_dirs()
            with open(config.SIGNUPS_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"{email}:{password}\n")
        except Exception:  # noqa: BLE001
            log.exception("Could not write to signups file")

    # ================================================================== #
    # This Round tab
    # ================================================================== #
    def _build_round_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=3)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="Lineups  (one per line, 5 names: 1st 2nd 3rd 4th 5th)",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=0, sticky="w", padx=PAD, pady=(PAD, 0))
        self.lineups_box = ctk.CTkTextbox(tab)
        self.lineups_box.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)

        ctk.CTkLabel(tab, text="Wildcards  (one per line)",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=1, sticky="w", padx=PAD, pady=(PAD, 0))
        self.wildcards_box = ctk.CTkTextbox(tab)
        self.wildcards_box.grid(row=1, column=1, sticky="nsew", padx=PAD, pady=PAD)

        # --- Start-at-account chooser -------------------------------- #
        start_card = ctk.CTkFrame(tab, fg_color=SURFACE_2, corner_radius=12)
        start_card.grid(row=2, column=0, columnspan=2, sticky="ew", padx=PAD, pady=(0, 4))
        start_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            start_card, text="Start at account:",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=FG,
        ).grid(row=0, column=0, padx=(14, 8), pady=10, sticky="w")
        self.start_at_combo = SearchableComboBox(
            start_card, values=[], width=420,
            fg_color=SURFACE, button_color=BRAND, button_hover_color=BRAND_HOVER,
            border_color=BORDER, dropdown_fg_color=SURFACE,
            dropdown_hover_color=HOVER, dropdown_text_color=FG,
        )
        self.start_at_combo.grid(row=0, column=1, padx=4, pady=10, sticky="w")
        ctk.CTkButton(
            start_card, text="First account", width=110, fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOV, command=self.on_start_at_first,
        ).grid(row=0, column=2, padx=(4, 14), pady=10)
        ctk.CTkLabel(
            start_card,
            text=("The round assigns from this account and goes DOWN the list. "
                  "Pick any account you own to skip ones already used in a "
                  "previous round (type to search)."),
            text_color=FG_MUTED, wraplength=1040, justify="left",
        ).grid(row=1, column=0, columnspan=3, padx=14, pady=(0, 10), sticky="w")

        controls = ctk.CTkFrame(tab, fg_color="transparent")
        controls.grid(row=3, column=0, columnspan=2, sticky="ew", padx=PAD)
        self.scrape_btn = ctk.CTkButton(controls, text="Scrape riders from site",
                                        fg_color=NEUTRAL, hover_color=NEUTRAL_HOV,
                                        command=self.on_scrape_roster)
        self.scrape_btn.pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Resolve & preview", fg_color=BRAND,
                      hover_color=BRAND_HOVER, command=self.on_resolve).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Fix ambiguous names", fg_color=WARNING,
                      hover_color=WARNING_HOV, command=self.on_fix_ambiguous).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Lock in assignments", fg_color=SUCCESS,
                      hover_color=SUCCESS_HOV, command=self.on_lock_plan).pack(side="left", padx=4)
        self.roster_lbl = ctk.CTkLabel(controls, text="Roster: (not scraped)", text_color=FG_MUTED)
        self.roster_lbl.pack(side="left", padx=12)

        self.round_summary = ctk.CTkLabel(tab, text="", anchor="w", text_color=BRAND)
        self.round_summary.grid(row=4, column=0, columnspan=2, sticky="ew", padx=PAD)

        self.preview_box = ctk.CTkTextbox(tab, height=170, fg_color=SURFACE_2)
        self.preview_box.grid(row=5, column=0, columnspan=2, sticky="ew", padx=PAD, pady=PAD)
        self.preview_box.configure(state="disabled")

    # --- Start-at-account helpers ------------------------------------ #
    def _refresh_start_at_choices(self) -> None:
        """Sync the This Round 'Start at account' dropdown with all accounts."""
        if not hasattr(self, "start_at_combo"):
            return
        self._round_accounts = self.repo.list_accounts()
        choices = [_fmt_account_choice(i, a) for i, a in enumerate(self._round_accounts)]
        prev = self.start_at_combo.get()
        self.start_at_combo.set_values(choices)
        # Keep the current selection if still valid; otherwise restore the
        # persisted start account (by id); otherwise default to the first.
        if prev in choices:
            self.start_at_combo.set(prev)
            return
        persisted = self.repo.get_meta("round_start_account_id")
        if persisted:
            try:
                pid = int(persisted)
                for i, a in enumerate(self._round_accounts):
                    if a.id == pid:
                        self.start_at_combo.set(choices[i])
                        return
            except (ValueError, TypeError):
                pass
        if choices:
            self.start_at_combo.set(choices[0])
        else:
            self.start_at_combo.set("")

    def _selected_start_offset(self) -> int:
        """0-based index into the full accounts list for the chosen start.

        Tolerant: matches the exact dropdown line first, then a substring
        (label / email / number the user typed), else defaults to 0.
        """
        accounts = self._round_accounts or self.repo.list_accounts()
        self._round_accounts = accounts
        if not accounts:
            return 0
        val = self.start_at_combo.get().strip().lower() if hasattr(self, "start_at_combo") else ""
        if not val:
            return 0
        choices = [_fmt_account_choice(i, a).lower() for i, a in enumerate(accounts)]
        for i, c in enumerate(choices):
            if c == val:
                return i
        for i, c in enumerate(choices):
            if val in c:
                return i
        return 0

    def on_start_at_first(self) -> None:
        if self._round_accounts:
            self.start_at_combo.set(_fmt_account_choice(0, self._round_accounts[0]))

    def _parse_lineups(self) -> list[list[str]]:
        lines = [ln.strip() for ln in self.lineups_box.get("1.0", "end").splitlines() if ln.strip()]
        return [ln.split() for ln in lines]

    def _parse_wildcards(self) -> list[str]:
        return [ln.strip() for ln in self.wildcards_box.get("1.0", "end").splitlines() if ln.strip()]

    def _ensure_resolver(self) -> bool:
        roster = self.repo.get_roster()
        if not roster:
            messagebox.showwarning(
                "No roster",
                "Scrape the rider roster from the site first (button on the left).",
            )
            return False
        self.resolver = RiderResolver(roster, self.repo.get_aliases())
        return True

    def on_resolve(self):
        if not self._ensure_resolver():
            return
        report, ok = self._build_resolution_report()
        self._set_preview(report)
        return ok

    def _record_problem(self, query, result) -> None:
        """Remember an ambiguous/unmatched query + its candidate riders."""
        key = (query or "").strip()
        if not key or key in self._problem_queries:
            return
        candidates = []
        if result.name:
            candidates.append(result.name)
        candidates += [n for n, _ in result.alternatives]
        self._problem_queries[key] = candidates

    def _build_resolution_report(self):
        """Resolve all lineups+wildcards; return (text_report, all_ok)."""
        assert self.resolver is not None
        lines = []
        all_ok = True
        self._problem_queries = {}

        raw_lineups = self._parse_lineups()
        lines.append("=== LINEUPS ===")
        resolved_lineups: list[list[str]] = []
        for i, tokens in enumerate(raw_lineups, 1):
            if len(tokens) != 5:
                all_ok = False
                lines.append(f"Lineup {i}: NEEDS 5 NAMES (got {len(tokens)}): {' '.join(tokens)}")
                resolved_lineups.append([])
                continue
            names = []
            parts = []
            for tok in tokens:
                r = self.resolver.resolve(tok)
                if r.ok:
                    parts.append(f"{tok}->{r.name}")
                    names.append(r.name)
                else:
                    all_ok = False
                    self._record_problem(tok, r)
                    flag = "AMBIGUOUS" if r.ambiguous else "NO MATCH"
                    alts = ", ".join(n for n, _ in r.alternatives[:2])
                    parts.append(f"{tok}->[{flag}: {r.name or '?'}{(' / ' + alts) if alts else ''}]")
                    names.append(r.name or f"?{tok}")
            resolved_lineups.append(names)
            lines.append(f"Lineup {i}: " + " | ".join(parts))

        lines.append("")
        lines.append("=== WILDCARDS ===")
        resolved_wildcards: list[str] = []
        for w in self._parse_wildcards():
            r = self.resolver.resolve(w)
            if r.ok:
                lines.append(f"{w} -> {r.name}  ({r.score})")
                resolved_wildcards.append(r.name)
            else:
                all_ok = False
                self._record_problem(w, r)
                flag = "AMBIGUOUS" if r.ambiguous else "NO MATCH"
                alts = ", ".join(n for n, _ in r.alternatives[:2])
                lines.append(f"{w} -> [{flag}: {r.name or '?'}{(' / ' + alts) if alts else ''}]")
                resolved_wildcards.append(r.name or f"?{w}")

        # Assignment math preview (honours the chosen start account).
        accounts = self.repo.list_accounts()
        self._round_accounts = accounts
        start_offset = self._selected_start_offset()
        plan = build_plan(resolved_lineups, resolved_wildcards, accounts, start_offset)
        self.round_summary.configure(text=plan.summary())
        self._pending_resolved = (resolved_lineups, resolved_wildcards)

        lines.insert(0, plan.summary())
        lines.insert(1, "")
        if not all_ok:
            lines.insert(0, ">>> Some names are unresolved/ambiguous - click 'Fix ambiguous names' to pin them. <<<")
        return "\n".join(lines), all_ok

    def _set_preview(self, text: str):
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", text)
        self.preview_box.configure(state="disabled")

    def on_fix_ambiguous(self):
        if not self._ensure_resolver():
            return
        report, _ok = self._build_resolution_report()
        self._set_preview(report)
        if not self._problem_queries:
            messagebox.showinfo(
                "Nothing to fix",
                "No ambiguous or unmatched names right now. If a name still "
                "resolves to the wrong rider, you can still pin it via this dialog.",
            )
        DisambiguationDialog(self, dict(self._problem_queries), self.resolver.roster)

    def _apply_aliases(self, mapping: dict[str, str]):
        """Persist the user's rider choices and re-resolve."""
        for query, rider in mapping.items():
            if rider:
                self.repo.set_alias(query, rider)
        # Rebuild the resolver so the new overrides take effect immediately.
        self.resolver = RiderResolver(self.repo.get_roster(), self.repo.get_aliases())
        report, ok = self._build_resolution_report()
        self._set_preview(report)
        if ok:
            messagebox.showinfo("Saved", "Overrides saved. All names now resolve cleanly.")

    def on_lock_plan(self):
        if not self._ensure_resolver():
            return
        report, ok = self._build_resolution_report()
        self._set_preview(report)
        if not ok:
            messagebox.showwarning("Cannot lock in",
                                   "Resolve all names first (see the preview for flags).")
            return
        resolved_lineups, resolved_wildcards = self._pending_resolved
        accounts = self.repo.list_accounts()
        if not accounts:
            messagebox.showwarning("No accounts", "Import accounts before locking in a plan.")
            return
        self._round_accounts = accounts
        start_offset = self._selected_start_offset()
        self.plan = build_plan(resolved_lineups, resolved_wildcards, accounts, start_offset)
        self._run_status = {}
        # The Run Picks "begin at" dropdown is a within-plan skip; reset to row 1.
        self._persist_round()
        self._persist_plan()
        self._persist_status()
        self._populate_run_table()
        self._set_run_start_position(1)

        start_label = ""
        if self.plan.assignments:
            first = self.plan.assignments[0]
            start_label = (f"\nFirst account: #{self.plan.start_offset + 1} "
                           f"{first.account_label} ({first.account_email}).")
        warn = ""
        if self.plan.unassigned_pairs:
            warn = (f"\n\nWARNING: {len(self.plan.unassigned_pairs)} pairs have no "
                    f"account from the start point onward (add more accounts or "
                    f"start higher up the list).")
        if self.plan.idle_accounts:
            warn += f"\n{len(self.plan.idle_accounts)} accounts will be idle."
        messagebox.showinfo(
            "Plan locked in",
            f"{self.plan.assigned_count} account submissions ready."
            f"{start_label}\nEach account submits its own assigned lineup + "
            f"wildcard, going down the list.{warn}\n\nGo to the 'Run Picks' tab.",
        )
        self.tabs.set("Run Picks")

    def on_scrape_roster(self):
        if self.scrape_thread and self.scrape_thread.is_alive():
            return
        accounts = self.repo.list_accounts(include_password=True)
        if not accounts:
            messagebox.showwarning("No accounts", "Import at least one account to scrape the roster.")
            return
        acc = accounts[0]
        self.scrape_btn.configure(state="disabled", text="Scraping...")
        self.roster_lbl.configure(text="Opening browser to scrape roster...")
        self.scrape_thread = threading.Thread(
            target=self._scrape_worker, args=(acc.id,), daemon=True
        )
        self.scrape_thread.start()

    def _scrape_worker(self, account_id: int):
        repo = Repository()
        try:
            acc = repo.get_account(account_id, include_password=True)
            with automation.chrome_session(acc.profile_dir, headless=False) as driver:
                self.events.put(("scrape_status", "Logging in / loading pick page..."))
                automation.ensure_logged_in(driver, acc.email, acc.password,
                                            status_cb=lambda m: self.events.put(("scrape_status", m)))
                driver.get(config.BASE_URL)
                roster = automation.scrape_roster(driver)
            repo.set_roster(roster)
            self.events.put(("scrape_done", len(roster)))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("scrape_error", str(exc)))
        finally:
            repo.close()

    # ================================================================== #
    # Run Picks tab
    # ================================================================== #
    def _build_run_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        opts = ctk.CTkFrame(tab)
        opts.grid(row=0, column=0, sticky="ew", padx=PAD, pady=PAD)
        opts.grid_columnconfigure(7, weight=1)

        ctk.CTkLabel(opts, text="Concurrent browsers:").grid(row=0, column=0, padx=(8, 4), pady=8)
        self.conc_value = ctk.CTkLabel(opts, text="3", width=28)
        self.conc_slider = ctk.CTkSlider(opts, from_=1, to=15, number_of_steps=14, width=150,
                                         command=lambda v: self.conc_value.configure(text=str(int(v))))
        self.conc_slider.set(3)
        self.conc_slider.grid(row=0, column=1, padx=4)
        self.conc_value.grid(row=0, column=2, padx=(0, 12))

        ctk.CTkLabel(opts, text="Launch stagger (s):").grid(row=0, column=3, padx=(8, 4))
        self.stagger_entry = ctk.CTkEntry(opts, width=54)
        self.stagger_entry.insert(0, "5")
        self.stagger_entry.grid(row=0, column=4, padx=4)

        ctk.CTkLabel(opts, text="Keep browser open after submit (s):").grid(row=0, column=5, padx=(8, 4))
        self.keepopen_entry = ctk.CTkEntry(opts, width=54)
        self.keepopen_entry.insert(0, "0.5")
        self.keepopen_entry.grid(row=0, column=6, padx=4)

        self.headless_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Headless", variable=self.headless_var
                        ).grid(row=0, column=7, padx=12, sticky="e")

        ctk.CTkLabel(opts, text="Begin run at:").grid(row=1, column=0, padx=(8, 4), pady=(0, 6), sticky="w")
        self.start_combo = SearchableComboBox(
            opts, values=[], width=300,
            fg_color=SURFACE, button_color=BRAND, button_hover_color=BRAND_HOVER,
            border_color=BORDER, dropdown_fg_color=SURFACE,
            dropdown_hover_color=HOVER, dropdown_text_color=FG,
        )
        self.start_combo.grid(row=1, column=1, columnspan=2, padx=4, pady=(0, 6), sticky="w")
        ctk.CTkButton(opts, text="Set from selected row", width=150, fg_color=NEUTRAL,
                      hover_color=NEUTRAL_HOV, command=self.on_set_start_from_selected
                      ).grid(row=1, column=3, padx=4, pady=(0, 6), sticky="w")
        ctk.CTkLabel(opts, text="(skips rows above this one; each account still submits its own assigned lineup + wildcard)",
                     text_color=FG_MUTED).grid(row=1, column=4, columnspan=4, padx=4, pady=(0, 6), sticky="w")

        ctk.CTkLabel(opts, text="Proxies (one host:port per line, optional; round-robin):"
                     ).grid(row=2, column=0, columnspan=8, sticky="w", padx=8)
        self.proxy_box = ctk.CTkTextbox(opts, height=44)
        self.proxy_box.grid(row=3, column=0, columnspan=8, sticky="ew", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            opts,
            text=("Avoid getting blocked (single IP, no proxies): keep it gentle - about "
                  "2-3 concurrent with a 5-8s stagger. If you get blocked/timeouts, drop to "
                  "1-2 with 10-15s and wait ~15 min. After the first run, saved logins mean "
                  "far fewer requests."),
            text_color="#9aa", wraplength=1040, justify="left",
        ).grid(row=4, column=0, columnspan=8, sticky="w", padx=8, pady=(0, 8))

        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=PAD)
        self.run_btn = ctk.CTkButton(actions, text="RUN PICKS", fg_color="#2c6e49",
                                     hover_color="#358257", height=40, width=130,
                                     font=ctk.CTkFont(size=15, weight="bold"), command=self.on_run)
        self.run_btn.pack(side="left", padx=4)
        self.retry_btn = ctk.CTkButton(actions, text="Retry failed", fg_color="#7a5a1e",
                                       hover_color="#8f6a26", height=40, width=100,
                                       command=self.on_retry_failed)
        self.retry_btn.pack(side="left", padx=4)
        self.stop_btn = ctk.CTkButton(actions, text="STOP", fg_color="#8a2c2c",
                                      hover_color="#a13636", height=40, width=80,
                                      state="disabled", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=4)
        self.watch_btn = ctk.CTkButton(actions, text="\U0001F441 Watch selected", width=140, height=40,
                                       command=self.on_watch_selected)
        self.watch_btn.pack(side="left", padx=4)
        self.reset_btn = ctk.CTkButton(actions, text="Reset round", fg_color="#555555",
                                       hover_color="#666666", height=40, width=100,
                                       command=self.on_reset_round)
        self.reset_btn.pack(side="left", padx=4)
        self.progress = ctk.CTkProgressBar(actions, width=220)
        self.progress.set(0)
        self.progress.pack(side="left", padx=12)
        self.progress_lbl = ctk.CTkLabel(actions, text="0 / 0")
        self.progress_lbl.pack(side="left")

        # Live per-account table.
        wrap = tk.Frame(tab, bg="#242424")
        wrap.grid(row=2, column=0, sticky="nsew", padx=PAD, pady=PAD)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        cols = ("num", "account", "lineup", "wildcard", "status")
        self.run_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, w, t, anchor in [
            ("num", 50, "#", "center"), ("account", 180, "Account", "w"),
            ("lineup", 70, "Lineup", "center"), ("wildcard", 150, "Wildcard", "w"),
            ("status", 430, "Status", "w"),
        ]:
            self.run_tree.heading(c, text=t)
            self.run_tree.column(c, width=w, anchor=anchor)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.run_tree.yview)
        self.run_tree.configure(yscrollcommand=vsb.set)
        self.run_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.run_tree.tag_configure("ok", foreground="#7fdf7f")
        self.run_tree.tag_configure("fail", foreground="#ff8a8a")
        self.run_tree.tag_configure("busy", foreground="#ffd27f")
        self.run_tree.tag_configure("skip", foreground="#888888")
        # Double-click a row = open a browser to watch that account.
        self.run_tree.bind("<Double-1>", lambda e: self.on_watch_selected())

    def _populate_run_table(self):
        for iid in self.run_tree.get_children():
            self.run_tree.delete(iid)
        self._run_row_by_account.clear()
        if not self.plan:
            self._refresh_run_start_choices()
            return
        for pos, a in enumerate(self.plan.assignments, 1):
            iid = f"acc{a.account_id}"
            self._run_row_by_account[a.account_id] = iid
            st = self._run_status.get(a.account_id)
            status_text = st[0] if st else "Pending"
            tag = st[1] if st and st[1] else ""
            self.run_tree.insert(
                "", "end", iid=iid,
                values=(pos, a.account_label, f"#{a.lineup_index}", a.wildcard, status_text),
                tags=(tag,) if tag else (),
            )
        self._refresh_run_start_choices()

    # --- Run-start (within-plan skip) helpers ------------------------ #
    def _run_start_choices(self) -> list[str]:
        if not self.plan:
            return []
        return [f"{i + 1}. {a.account_label}  ({a.account_email})"
                for i, a in enumerate(self.plan.assignments)]

    def _refresh_run_start_choices(self) -> None:
        if not hasattr(self, "start_combo"):
            return
        choices = self._run_start_choices()
        prev = self.start_combo.get()
        self.start_combo.set_values(choices)
        if prev in choices:
            self.start_combo.set(prev)
        elif choices:
            self.start_combo.set(choices[0])
        else:
            self.start_combo.set("")

    def _set_run_start_position(self, pos: int) -> None:
        """Select the given 1-based plan position in the Run Picks dropdown."""
        if not hasattr(self, "start_combo"):
            return
        choices = self.start_combo.all_values() or self._run_start_choices()
        if 1 <= pos <= len(choices):
            self.start_combo.set(choices[pos - 1])
        elif choices:
            self.start_combo.set(choices[0])

    def _selected_run_start_position(self) -> int:
        """Parse the 1-based plan position from the Run Picks dropdown value."""
        if not hasattr(self, "start_combo"):
            return 1
        val = self.start_combo.get().strip()
        digits = ""
        for ch in val:
            if ch.isdigit():
                digits += ch
            else:
                break
        try:
            return max(1, int(digits))
        except ValueError:
            return 1

    def _read_run_options(self):
        try:
            stagger = float(self.stagger_entry.get() or "1.0")
        except ValueError:
            stagger = 1.0
        try:
            keep_open = float(self.keepopen_entry.get() or "3")
        except ValueError:
            keep_open = 3.0
        proxies = [ln.strip() for ln in self.proxy_box.get("1.0", "end").splitlines() if ln.strip()]
        return stagger, keep_open, proxies

    def on_run(self):
        if self.run_thread and self.run_thread.is_alive():
            return
        if not self.plan or not self.plan.assignments:
            messagebox.showwarning("No plan", "Lock in a plan on the 'This Round' tab first.")
            return
        assignments = list(self.plan.assignments)
        start = self._selected_run_start_position()
        start = max(1, min(start, len(assignments)))
        active = assignments[start - 1:]
        # Reset row states: mark skipped ones before the start, others Pending.
        for i, a in enumerate(assignments):
            if i < start - 1:
                self._update_run_row(a.account_id, "Skipped (start offset)", "skip")
            else:
                self._update_run_row(a.account_id, "Pending", "")
        self._persist_status()
        self._launch_run(active)

    def on_retry_failed(self):
        if self.run_thread and self.run_thread.is_alive():
            return
        if not self.plan:
            messagebox.showwarning("No plan", "Nothing to retry yet.")
            return
        failed = {aid for aid, st in self._run_status.items() if st and st[1] == "fail"}
        subset = [a for a in self.plan.assignments if a.account_id in failed]
        if not subset:
            messagebox.showinfo("Nothing to retry", "There are no failed accounts to retry.")
            return
        for a in subset:
            self._update_run_row(a.account_id, "Pending (retry)", "")
        self._persist_status()
        self._launch_run(subset)

    def _launch_run(self, assignments):
        stagger, keep_open, proxies = self._read_run_options()
        self.progress.set(0)
        self.progress_lbl.configure(text=f"0 / {len(assignments)}")
        self._set_running(True)
        self.runner = ConcurrentRunner(
            concurrency=int(self.conc_slider.get()),
            headless=self.headless_var.get(),
            launch_stagger=stagger,
            proxies=proxies,
            post_submit_dwell=keep_open,
        )
        self.run_thread = threading.Thread(
            target=self._run_worker, args=(list(assignments),), daemon=True
        )
        self.run_thread.start()

    def on_set_start_from_selected(self):
        sel = self.run_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a row in the table first.")
            return
        num = self.run_tree.set(sel[0], "num")
        try:
            self._set_run_start_position(int(num))
        except (ValueError, TypeError):
            pass

    def on_watch_selected(self):
        if self.run_thread and self.run_thread.is_alive():
            messagebox.showinfo(
                "Run in progress",
                "Finish/stop the run before opening a browser - the account "
                "profiles are in use while a run is going.",
            )
            return
        sel = self.run_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select an account row, then click the eye (or double-click a row).")
            return
        iid = sel[0]
        if not iid.startswith("acc"):
            return
        account_id = int(iid[3:])
        self._update_run_row(account_id, "Opening browser...", "busy", remember=False)
        t = threading.Thread(target=self._watch_worker, args=(account_id,), daemon=True)
        self._watch_threads.append(t)
        t.start()

    def _watch_worker(self, account_id: int):
        repo = Repository()
        try:
            acc = repo.get_account(account_id, include_password=True)
            if acc is None:
                self.events.put(("watch_error", account_id, "Account not found."))
                return
            driver = automation.build_driver(acc.profile_dir, headless=False)
            try:
                automation.ensure_logged_in(
                    driver, acc.email, acc.password,
                    status_cb=lambda m: self.events.put(("watch_status", account_id, m)),
                )
                driver.get(config.BASE_URL)
                self.events.put(("watch_status", account_id, "Browser open - close the window when done."))
                # Keep the session alive until the user closes the window.
                while True:
                    try:
                        if not driver.window_handles:
                            break
                    except Exception:
                        break
                    time.sleep(1.0)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
            self.events.put(("watch_status", account_id, "Browser closed."))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("watch_error", account_id, str(exc)))
        finally:
            repo.close()

    def on_reset_round(self):
        if self.run_thread and self.run_thread.is_alive():
            messagebox.showinfo("Run in progress", "Stop the run before resetting the round.")
            return
        if not messagebox.askyesno(
            "Reset round",
            "Start a fresh round?\n\n"
            "CLEARS: lineups & wildcards, the locked plan, the Run Picks table, "
            "and the submission History.\n\n"
            "KEEPS: your accounts and saved name overrides.\n\n"
            "(Until you reset here, everything stays put across app restarts.)",
        ):
            return
        self.repo.reset_round()
        self.plan = None
        self._run_status = {}
        for iid in self.run_tree.get_children():
            self.run_tree.delete(iid)
        self._run_row_by_account.clear()
        self.lineups_box.delete("1.0", "end")
        self.wildcards_box.delete("1.0", "end")
        self._set_preview("")
        self.round_summary.configure(text="")
        self.progress.set(0)
        self.progress_lbl.configure(text="0 / 0")
        self.refresh_history()
        messagebox.showinfo("Round reset", "Cleared. Set up the new round on the 'This Round' tab.")

    def _run_worker(self, assignments):
        cb = RunCallbacks(
            on_task_start=lambda a: self.events.put(("task_start", a.account_id)),
            on_status=lambda aid, m: self.events.put(("status", aid, m)),
            on_result=lambda r: self.events.put(("result", r)),
            on_progress=lambda d, t: self.events.put(("progress", d, t)),
        )
        try:
            self.runner.run(assignments, cb)
            self.events.put(("run_done",))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("run_error", str(exc)))

    def on_stop(self):
        if self.runner:
            self.runner.cancel()
            self.stop_btn.configure(state="disabled", text="Stopping...")

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        for btn in (self.run_btn, self.retry_btn, self.watch_btn, self.reset_btn):
            btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled", text="STOP")

    # ================================================================== #
    # History tab
    # ================================================================== #
    def _build_history_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=PAD, pady=PAD)
        ctk.CTkButton(bar, text="Refresh", command=self.refresh_history).pack(side="left", padx=4)
        self.history_count = ctk.CTkLabel(bar, text="")
        self.history_count.pack(side="left", padx=12)

        wrap = tk.Frame(tab, bg="#242424")
        wrap.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bar, text="(double-click a row to see the full 5)", text_color=FG_MUTED,
        ).pack(side="left", padx=8)

        cols = ("time", "account", "lineup", "core", "wildcard", "ok", "message")
        self.history_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, w, t in [
            ("time", 130, "Time"), ("account", 130, "Account"), ("lineup", 60, "Lineup"),
            ("core", 300, "Top 5 (1st\u21925th)"), ("wildcard", 130, "Wildcard"),
            ("ok", 60, "Result"), ("message", 300, "Message"),
        ]:
            self.history_tree.heading(c, text=t)
            self.history_tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=vsb.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.history_tree.tag_configure("ok", foreground=ROW_OK)
        self.history_tree.tag_configure("fail", foreground=ROW_FAIL)
        # Remember each row's full details for the double-click popup.
        self._history_by_iid: dict[str, object] = {}
        self.history_tree.bind("<Double-1>", lambda e: self._show_history_detail())

    def refresh_history(self):
        for iid in self.history_tree.get_children():
            self.history_tree.delete(iid)
        self._history_by_iid = {}
        logs = self.repo.list_submission_logs()
        for i, e in enumerate(logs):
            iid = f"hist{i}"
            self._history_by_iid[iid] = e
            self.history_tree.insert(
                "", "end", iid=iid,
                values=(e.timestamp, e.account_label, f"#{e.round_number}",
                        e.core_five or "(not recorded)", e.wildcard,
                        "OK" if e.success else "FAIL", e.message),
                tags=("ok" if e.success else "fail",),
            )
        self.history_count.configure(text=f"{len(logs)} submissions")

    def _show_history_detail(self):
        sel = self.history_tree.selection()
        if not sel:
            return
        e = self._history_by_iid.get(sel[0])
        if e is None:
            return
        riders = [r.strip() for r in (e.core_five or "").split(",") if r.strip()]
        if riders:
            places = ["1st", "2nd", "3rd", "4th", "5th"]
            lines = [f"  {places[i] if i < len(places) else str(i + 1) + 'th'}: {r}"
                     for i, r in enumerate(riders)]
            core_txt = "\n".join(lines)
        else:
            core_txt = "  (the 5 riders weren't recorded for this entry)"
        messagebox.showinfo(
            f"Lineup #{e.round_number} - {e.account_label}",
            f"Account: {e.account_label} ({e.account_email})\n"
            f"Submitted: {e.timestamp}\n"
            f"Result: {'OK' if e.success else 'FAIL'}\n\n"
            f"Top 5 (core):\n{core_txt}\n\n"
            f"Wild card (13th): {e.wildcard}\n\n"
            f"{e.message}",
        )

    # ================================================================== #
    # Persistence of round inputs
    # ================================================================== #
    def _persist_round(self):
        self.repo.set_meta("round_lineups_text", self.lineups_box.get("1.0", "end").strip())
        self.repo.set_meta("round_wildcards_text", self.wildcards_box.get("1.0", "end").strip())
        # Remember which account the round starts at (by id, so it survives
        # reordering / re-import as long as that account still exists).
        accounts = self._round_accounts or self.repo.list_accounts()
        off = self._selected_start_offset()
        if accounts and 0 <= off < len(accounts):
            self.repo.set_meta("round_start_account_id", str(accounts[off].id))

    def _persist_plan(self):
        if self.plan:
            self.repo.set_meta("round_plan_json", json.dumps(_plan_to_dict(self.plan)))

    def _persist_status(self):
        self.repo.set_meta(
            "round_status_json",
            json.dumps({str(k): list(v) for k, v in self._run_status.items()}),
        )

    def _load_persisted(self):
        lu = self.repo.get_meta("round_lineups_text")
        wc = self.repo.get_meta("round_wildcards_text")
        if lu:
            self.lineups_box.insert("1.0", lu)
        if wc:
            self.wildcards_box.insert("1.0", wc)
        roster = self.repo.get_roster()
        if roster:
            self.roster_lbl.configure(
                text=f"Roster: {len(roster)} riders (updated {self.repo.roster_updated_at()})"
            )
        # Restore per-account run statuses (survives restart until Reset round).
        self._run_status = {}
        status_json = self.repo.get_meta("round_status_json")
        if status_json:
            try:
                self._run_status = {int(k): tuple(v) for k, v in json.loads(status_json).items()}
            except Exception:
                self._run_status = {}
        # Restore the locked plan + repopulate the Run Picks table.
        plan_json = self.repo.get_meta("round_plan_json")
        if plan_json:
            try:
                self.plan = _plan_from_dict(json.loads(plan_json))
            except Exception:
                self.plan = None
        if self.plan:
            self._populate_run_table()
            self.round_summary.configure(text=self.plan.summary())

    # ================================================================== #
    # Event pump (runs on the Tk main thread)
    # ================================================================== #
    def _drain_events(self):
        try:
            while True:
                evt = self.events.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self._drain_after_id = self.after(100, self._drain_events)

    def _handle_event(self, evt):
        kind = evt[0]
        if kind == "scrape_status":
            self.roster_lbl.configure(text=evt[1])
        elif kind == "scrape_done":
            count = evt[1]
            self.scrape_btn.configure(state="normal", text="Scrape riders from site")
            self.roster_lbl.configure(text=f"Roster: {count} riders (updated {self.repo.roster_updated_at()})")
            self.resolver = RiderResolver(self.repo.get_roster(), self.repo.get_aliases())
            messagebox.showinfo("Roster scraped", f"Cached {count} riders. You can now Resolve & preview.")
        elif kind == "scrape_error":
            self.scrape_btn.configure(state="normal", text="Scrape riders from site")
            self.roster_lbl.configure(text="Scrape failed.")
            messagebox.showerror("Scrape failed", evt[1])
        elif kind == "task_start":
            self._update_run_row(evt[1], status="Starting...", tag="busy")
        elif kind == "status":
            self._update_run_row(evt[1], status=evt[2], tag="busy")
        elif kind == "result":
            r: RunResult = evt[1]
            self._update_run_row(r.account_id, status=r.message,
                                 tag="ok" if r.success else "fail")
            self._persist_status()
        elif kind == "progress":
            done, total = evt[1], evt[2]
            self.progress.set(done / total if total else 0)
            self.progress_lbl.configure(text=f"{done} / {total}")
        elif kind == "watch_status":
            self._update_run_row(evt[1], status=evt[2], tag="busy", remember=False)
        elif kind == "watch_error":
            self._update_run_row(evt[1], status=f"Browser error: {evt[2]}", tag="fail", remember=False)
            messagebox.showerror("Browser error", evt[2])
        elif kind == "run_done":
            self._on_run_finished()
        elif kind == "run_error":
            self._on_run_finished()
            messagebox.showerror("Run error", evt[1])
        elif kind == "su_task_start":
            self._su_update_row(evt[1], "Starting...", "busy")
        elif kind == "su_status":
            self._su_update_row(evt[1], evt[2], "busy")
        elif kind == "su_result":
            r: SignupResult = evt[1]
            tag = "ok" if r.success else ("skip" if "skipped" in r.message.lower() else "fail")
            self._su_update_row(r.email, r.message, tag)
            if r.success and r.password:
                self._append_signup_file(r.email, r.password)
                self.refresh_accounts()
        elif kind == "su_progress":
            done, total = evt[1], evt[2]
            self.signup_progress.set(done / total if total else 0)
            self.signup_progress_lbl.configure(text=f"{done} / {total}")
        elif kind == "su_run_done":
            self._on_signup_finished()
        elif kind == "su_run_error":
            self._on_signup_finished()
            messagebox.showerror("Sign-up error", evt[1])
        elif kind == "proxy_ip":
            self.test_proxy_btn.configure(state="normal", text="Test proxy")
            self.signup_status_lbl.configure(text=f"Proxy public IP: {evt[1]}")
            messagebox.showinfo(
                "Proxy test",
                f"The browser's public IP through the proxy was:\n\n{evt[1]}\n\n"
                "If this is NOT your normal home IP, the proxy is working.",
            )
        elif kind == "proxy_err":
            self.test_proxy_btn.configure(state="normal", text="Test proxy")
            self.signup_status_lbl.configure(text="Proxy test failed.")
            messagebox.showerror(
                "Proxy test failed",
                f"Could not load a page through the proxy:\n\n{evt[1]}\n\n"
                "Check the host:port:user:pass and that the proxy is active.",
            )
        elif kind == "debug_dump":
            self.debug_form_btn.configure(state="normal", text="Debug form")
            path, report = evt[1], evt[2]
            self.signup_status_lbl.configure(text=f"Form dump written to: {path}")
            head = report[:1200] + ("\n... (full dump in the file above)" if len(report) > 1200 else "")
            messagebox.showinfo(
                "Sign-up form dump",
                f"Saved the form's buttons + fields to:\n{path}\n\n"
                f"Open that file and share it with me.\n\n{head}",
            )
        elif kind == "debug_err":
            self.debug_form_btn.configure(state="normal", text="Debug form")
            self.signup_status_lbl.configure(text="Form dump failed.")
            messagebox.showerror("Debug form failed", str(evt[1]))
        elif kind == "sl_status":
            acc = self.repo.get_account(evt[1], include_password=False)
            label = acc.label if acc else f"#{evt[1]}"
            self.accounts_status.configure(text=f"[{label}] {evt[2]}")
        elif kind == "sl_result":
            self.refresh_accounts()
        elif kind == "sl_progress":
            self.accounts_status.configure(text=f"Auto-fill login... {evt[1]}/{evt[2]}")
        elif kind == "sl_done":
            self.assist_login_btn.configure(state="normal", text="Log in selected (auto-fill)")
            self.assist_stop_btn.configure(state="disabled", text="Stop")
            self.refresh_accounts()
            self.accounts_status.configure(text="Auto-fill login run finished.")
        elif kind == "sl_error":
            self.assist_login_btn.configure(state="normal", text="Log in selected (auto-fill)")
            self.assist_stop_btn.configure(state="disabled", text="Stop")
            self.refresh_accounts()
            messagebox.showerror("Login error", str(evt[1]))
        elif kind == "chrome_opened":
            self.accounts_status.configure(
                text=f"Opened {evt[1]} Chrome window(s). Log in, close them, then "
                     f"click 'Refresh login status'."
            )
        elif kind == "si_result":
            self.refresh_accounts()
        elif kind == "si_progress":
            self.accounts_status.configure(text=f"Checking login status... {evt[1]}/{evt[2]}")
        elif kind == "si_done":
            self.refresh_logins_btn.configure(state="normal", text="Refresh login status")
            self.open_chrome_btn.configure(state="normal")
            self.refresh_accounts()
            valid = sum(1 for a in self.repo.list_accounts() if a.session_valid)
            self.accounts_status.configure(text=f"Login check done. {valid} account(s) logged in.")
        elif kind == "si_error":
            self.refresh_logins_btn.configure(state="normal", text="Refresh login status")
            self.open_chrome_btn.configure(state="normal")
            self.refresh_accounts()
            messagebox.showerror("Login check error", str(evt[1]))

    def _update_run_row(self, account_id: int, status: str, tag: str, remember: bool = True):
        iid = self._run_row_by_account.get(account_id)
        if iid and self.run_tree.exists(iid):
            self.run_tree.set(iid, "status", status)
            self.run_tree.item(iid, tags=(tag,) if tag else ())
        # ``remember`` keeps the persisted round state clean (watch actions
        # update only the visible row, not the saved per-account result).
        if remember:
            self._run_status[account_id] = (status, tag)

    def _on_run_finished(self):
        self._set_running(False)
        self._persist_status()
        self.refresh_accounts()
        self.refresh_history()

    def _on_signup_finished(self):
        self._set_signup_running(False)
        self.refresh_accounts()
        n = sum(
            1 for iid in self.signup_tree.get_children()
            if "ok" in (self.signup_tree.item(iid, "tags") or ())
        )
        self.signup_status_lbl.configure(
            text=f"Done. {n} new account(s) saved to {config.SIGNUPS_PATH}"
        )

    def _on_close(self):
        try:
            # Stop the event pump so no pending timer fires after destroy().
            if getattr(self, "_drain_after_id", None):
                try:
                    self.after_cancel(self._drain_after_id)
                except Exception:
                    pass
            if self.runner:
                self.runner.cancel()
            if self.signup_runner:
                self.signup_runner.cancel()
            if self.signin_runner:
                self.signin_runner.cancel()
            if self.assist_runner:
                self.assist_runner.cancel()
            # Persist the round so nothing is lost on close (cleared only by Reset round).
            self._persist_round()
            self._persist_plan()
            self._persist_status()
            self.repo.close()
        finally:
            self.destroy()


class EditAccountDialog(ctk.CTkToplevel):
    def __init__(self, app: "App", account_id: int, label: str, email: str):
        super().__init__(app)
        self.app = app
        self.account_id = account_id
        self.title("Edit account")
        self.geometry("380x260")
        self.grab_set()

        ctk.CTkLabel(self, text="Label:").pack(anchor="w", padx=16, pady=(16, 0))
        self.label_entry = ctk.CTkEntry(self, width=320)
        self.label_entry.insert(0, label)
        self.label_entry.pack(padx=16)

        ctk.CTkLabel(self, text="Email:").pack(anchor="w", padx=16, pady=(8, 0))
        self.email_entry = ctk.CTkEntry(self, width=320)
        self.email_entry.insert(0, email)
        self.email_entry.pack(padx=16)

        ctk.CTkLabel(self, text="New password (blank = keep current):").pack(anchor="w", padx=16, pady=(8, 0))
        self.pw_entry = ctk.CTkEntry(self, width=320, show="*")
        self.pw_entry.pack(padx=16)

        ctk.CTkButton(self, text="Save", command=self._save).pack(pady=16)

    def _save(self):
        label = self.label_entry.get().strip()
        email = self.email_entry.get().strip()
        pw = self.pw_entry.get()
        if not label or not email:
            messagebox.showwarning("Missing", "Label and email are required.", parent=self)
            return
        self.app.repo.update_account(self.account_id, label, email, pw or None)
        self.app.refresh_accounts()
        self.destroy()


class DisambiguationDialog(ctk.CTkToplevel):
    """Pick the correct rider for each ambiguous/unmatched name.

    Choices are saved as persistent aliases, so e.g. 'Jorge' -> 'Jorge Prado'
    is remembered for every future round.
    """

    def __init__(self, app: "App", problems: dict[str, list[str]], roster: list[str]):
        super().__init__(app)
        self.app = app
        self.title("Fix ambiguous / unmatched names")
        self.geometry("600x540")
        self.grab_set()

        ctk.CTkLabel(
            self,
            text="Pick the correct rider for each name below. Your choices are "
                 "saved and reused automatically every round. You can also type "
                 "to search the dropdown.",
            wraplength=560, justify="left",
        ).pack(padx=16, pady=(16, 8), anchor="w")

        frame = ctk.CTkScrollableFrame(self, height=380)
        frame.pack(fill="both", expand=True, padx=16, pady=8)

        self.rows: dict[str, ctk.CTkComboBox] = {}
        if not problems:
            ctk.CTkLabel(frame, text="(nothing flagged - pin any name manually below is unavailable)"
                         ).pack(anchor="w", pady=6)

        for query, candidates in problems.items():
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=query, width=150, anchor="w").pack(side="left", padx=(0, 8))
            # Candidates first (best guesses), then the rest of the roster.
            opts, seen = [], set()
            for c in list(candidates) + list(roster):
                if c and c not in seen:
                    seen.add(c)
                    opts.append(c)
            combo = ctk.CTkComboBox(row, values=opts, width=360)
            combo.set(candidates[0] if candidates else (opts[0] if opts else ""))
            combo.pack(side="left")
            self.rows[query] = combo

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(btns, text="Save choices", fg_color="#2c6e49", hover_color="#358257",
                      command=self._save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)

    def _save(self):
        mapping = {q: combo.get().strip() for q, combo in self.rows.items()}
        self.destroy()
        self.app._apply_aliases(mapping)


class StartFromDialog(ctk.CTkToplevel):
    """Ask which account position the run should start from (1 = first)."""

    def __init__(self, app: "App", max_n: int, default: int = 1):
        super().__init__(app)
        self.title("Start from account #")
        self.geometry("380x210")
        self.max_n = max(1, max_n)
        self.result: int | None = default
        self.transient(app)

        ctk.CTkLabel(
            self,
            text=(f"Which account position should the run start from?\n\n"
                  f"1 = the first account (up to {self.max_n}). Rows above your "
                  f"number are skipped; every account still submits its own "
                  f"assigned lineup + wildcard."),
            wraplength=340, justify="left",
        ).pack(padx=16, pady=(16, 8))

        self.entry = ctk.CTkEntry(self, width=100, justify="center")
        self.entry.insert(0, str(default))
        self.entry.pack(pady=4)
        self.entry.focus_set()
        self.entry.select_range(0, "end")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=14)
        ctk.CTkButton(btns, text="OK", width=90, command=self._ok).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Cancel (use 1)", width=110, fg_color="#555555",
                      hover_color="#666666", command=self._cancel).pack(side="left", padx=6)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        # Defer grab until the window is viewable to avoid a grab error.
        self.after(50, self._safe_grab)

    def _safe_grab(self):
        try:
            self.grab_set()
        except Exception:
            pass

    def _ok(self):
        try:
            v = int(self.entry.get())
        except ValueError:
            v = 1
        self.result = max(1, min(v, self.max_n))
        self.destroy()

    def _cancel(self):
        self.result = 1
        self.destroy()


def main() -> None:
    from ..logging_setup import setup_logging
    setup_logging()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
