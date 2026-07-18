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
import queue
import threading
import time
import tkinter as tk
from dataclasses import asdict
from tkinter import messagebox, ttk

import customtkinter as ctk

from .. import automation, config
from ..assignment import AssignmentPlan, RoundAssignment, build_plan
from ..repository import Repository
from ..resolver import RiderResolver
from ..runner import ConcurrentRunner, RunCallbacks, RunResult

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

PAD = 8


def _plan_to_dict(plan: AssignmentPlan) -> dict:
    """Serialize an AssignmentPlan for persistence between app runs."""
    return {
        "lineup_count": plan.lineup_count,
        "wildcard_count": plan.wildcard_count,
        "pairs_needed": plan.pairs_needed,
        "accounts_available": plan.accounts_available,
        "unassigned_pairs": [list(p) for p in plan.unassigned_pairs],
        "idle_accounts": list(plan.idle_accounts),
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
    )


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RMFantasySMX Pick Bot")
        self.geometry("1120x760")
        self.minsize(960, 640)

        config.ensure_dirs()
        self.repo = Repository()  # main-thread connection

        # Shared run state.
        self.resolver: RiderResolver | None = None
        self.plan: AssignmentPlan | None = None
        self.round_label = "Current round"
        self.events: queue.Queue = queue.Queue()
        self.run_thread: threading.Thread | None = None
        self.scrape_thread: threading.Thread | None = None
        self.runner: ConcurrentRunner | None = None
        self._run_row_by_account: dict[int, str] = {}
        self._run_status: dict[int, tuple] = {}   # account_id -> (status_text, tag)
        self._watch_threads: list = []

        self._style_treeview()

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.tabs.add("Accounts")
        self.tabs.add("This Round")
        self.tabs.add("Run Picks")
        self.tabs.add("History")

        self._build_accounts_tab(self.tabs.tab("Accounts"))
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
            background="#242424", foreground="#e6e6e6", fieldbackground="#242424",
            rowheight=24, borderwidth=0,
        )
        style.configure("Treeview.Heading", background="#1a1a1a", foreground="#dddddd")
        style.map("Treeview", background=[("selected", "#2a5d9c")])

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
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.accounts_tree.yview)
        self.accounts_tree.configure(yscrollcommand=vsb.set)
        self.accounts_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        acc_btns = ctk.CTkFrame(right, fg_color="transparent")
        acc_btns.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        ctk.CTkButton(acc_btns, text="Rename / change password",
                      command=self.on_edit_account).pack(side="left", padx=4)
        ctk.CTkButton(acc_btns, text="Remove selected", fg_color="#8a2c2c",
                      hover_color="#a13636", command=self.on_remove_account).pack(side="left", padx=4)

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
        for acc in accounts:
            self.accounts_tree.insert(
                "", "end", iid=str(acc.id), text=acc.label,
                values=(acc.email, "valid" if acc.session_valid else "-"),
            )
        self.accounts_count_lbl.configure(text=f"Accounts: {len(accounts)}")

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

        controls = ctk.CTkFrame(tab, fg_color="transparent")
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", padx=PAD)
        self.scrape_btn = ctk.CTkButton(controls, text="Scrape riders from site",
                                        command=self.on_scrape_roster)
        self.scrape_btn.pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Resolve & preview", command=self.on_resolve).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Fix ambiguous names", fg_color="#7a5a1e",
                      hover_color="#8f6a26", command=self.on_fix_ambiguous).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Lock in assignments", fg_color="#2c6e49",
                      hover_color="#358257", command=self.on_lock_plan).pack(side="left", padx=4)
        self.roster_lbl = ctk.CTkLabel(controls, text="Roster: (not scraped)")
        self.roster_lbl.pack(side="left", padx=12)

        self.round_summary = ctk.CTkLabel(tab, text="", anchor="w", text_color="#d0d0ff")
        self.round_summary.grid(row=3, column=0, columnspan=2, sticky="ew", padx=PAD)

        self.preview_box = ctk.CTkTextbox(tab, height=170)
        self.preview_box.grid(row=4, column=0, columnspan=2, sticky="ew", padx=PAD, pady=PAD)
        self.preview_box.configure(state="disabled")

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

        # Assignment math preview.
        accounts = self.repo.list_accounts()
        plan = build_plan(resolved_lineups, resolved_wildcards, accounts)
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
        self.plan = build_plan(resolved_lineups, resolved_wildcards, accounts)
        self._run_status = {}
        self._persist_round()
        self._persist_plan()
        self._persist_status()
        self._populate_run_table()

        # Ask which account position to start from (1 = first, default).
        # This just sets the same "Start from account #" field on Run Picks,
        # which you can still change there before running.
        dlg = StartFromDialog(self, max_n=self.plan.assigned_count, default=1)
        self.wait_window(dlg)
        start = dlg.result if dlg.result is not None else 1
        self.start_entry.delete(0, "end")
        self.start_entry.insert(0, str(start))

        warn = ""
        if self.plan.unassigned_pairs:
            warn = f"\n\nWARNING: {len(self.plan.unassigned_pairs)} pairs have no account (need more accounts)."
        if self.plan.idle_accounts:
            warn += f"\n{len(self.plan.idle_accounts)} accounts will be idle."
        messagebox.showinfo(
            "Plan locked in",
            f"{self.plan.assigned_count} account submissions ready.\n"
            f"Run starts at account #{start} - each account submits its own "
            f"assigned lineup + wildcard.{warn}\n\nGo to the 'Run Picks' tab.",
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

        ctk.CTkLabel(opts, text="Start from account #:").grid(row=1, column=0, padx=(8, 4), pady=(0, 6), sticky="w")
        self.start_entry = ctk.CTkEntry(opts, width=70)
        self.start_entry.insert(0, "1")
        self.start_entry.grid(row=1, column=1, padx=4, pady=(0, 6), sticky="w")
        ctk.CTkButton(opts, text="Set from selected row", width=150,
                      command=self.on_set_start_from_selected
                      ).grid(row=1, column=2, columnspan=2, padx=4, pady=(0, 6), sticky="w")
        ctk.CTkLabel(opts, text="(skips rows above this position; each account still submits its own assigned lineup + wildcard)",
                     text_color="#9aa").grid(row=1, column=4, columnspan=4, padx=4, pady=(0, 6), sticky="w")

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
        try:
            start = int(self.start_entry.get() or "1")
        except ValueError:
            start = 1
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
        self.start_entry.delete(0, "end")
        self.start_entry.insert(0, str(num))

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
        cols = ("time", "account", "lineup", "wildcard", "ok", "message")
        self.history_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, w, t in [
            ("time", 140, "Time"), ("account", 150, "Account"), ("lineup", 80, "Lineup"),
            ("wildcard", 150, "Wildcard"), ("ok", 70, "Result"), ("message", 380, "Message"),
        ]:
            self.history_tree.heading(c, text=t)
            self.history_tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=vsb.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.history_tree.tag_configure("ok", foreground="#7fdf7f")
        self.history_tree.tag_configure("fail", foreground="#ff8a8a")

    def refresh_history(self):
        for iid in self.history_tree.get_children():
            self.history_tree.delete(iid)
        logs = self.repo.list_submission_logs()
        for e in logs:
            self.history_tree.insert(
                "", "end",
                values=(e.timestamp, e.account_label, f"#{e.round_number}", e.wildcard,
                        "OK" if e.success else "FAIL", e.message),
                tags=("ok" if e.success else "fail",),
            )
        self.history_count.configure(text=f"{len(logs)} submissions")

    # ================================================================== #
    # Persistence of round inputs
    # ================================================================== #
    def _persist_round(self):
        self.repo.set_meta("round_lineups_text", self.lineups_box.get("1.0", "end").strip())
        self.repo.set_meta("round_wildcards_text", self.wildcards_box.get("1.0", "end").strip())

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
