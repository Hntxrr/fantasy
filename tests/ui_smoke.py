"""Construct the full app under a virtual display, then close it.

Validates that every tab/widget builds and the event pump starts without error,
and exercises bulk import -> roster -> resolve -> plan -> run table wiring.
Run under Xvfb:  xvfb-run -a python tests/ui_smoke.py
"""
import os
os.environ.setdefault("RMFANTASY_HOME", "/tmp/rmf_ui_smoke")

import rmfantasy.ui.app as appmod
from rmfantasy.ui.app import App
from rmfantasy.assignment import build_plan

# Stub modal dialogs so they don't block this headless smoke run.
appmod.messagebox.showinfo = lambda *a, **k: None
appmod.messagebox.showwarning = lambda *a, **k: None
appmod.messagebox.showerror = lambda *a, **k: None
appmod.messagebox.askyesno = lambda *a, **k: True

app = App()

app.repo.clear_all_accounts()
res = app.repo.bulk_import_accounts("\n".join(f"user{i}@ex.com:pw{i}" for i in range(16)))
print("bulk import:", res.summary())
app.refresh_accounts()

app.repo.set_roster([
    "Jett Lawrence", "Hunter Lawrence", "Haiden Deegan", "Eli Tomac", "Jorge Prado",
    "Jorge Rubalcava", "Matti Jorgensen",  # make "Jorge" ambiguous
    "Jordon Smith", "Valentin Guillod", "Justin Barcia", "Mitchell Harrison",
    "Antonio Cairoli", "Cooper Webb", "Chase Sexton", "Aaron Plessinger",
])

app.lineups_box.delete("1.0", "end")
app.lineups_box.insert("1.0", "Jett Hunter Haiden Eli Jorge\nHunter Jett Haiden Jorge Eli")
app.wildcards_box.delete("1.0", "end")
app.wildcards_box.insert("1.0", "Jordan smith\nValentine\nJustin barcia\nMitchell harrison")

ok = app.on_resolve()
print("resolve all ok (expect False, Jorge ambiguous):", ok)
print("problem queries:", list(app._problem_queries.keys()))

# Build the disambiguation dialog, then apply the override programmatically.
from rmfantasy.ui.app import DisambiguationDialog
dlg = DisambiguationDialog(app, dict(app._problem_queries), app.resolver.roster)
print("dialog rows:", list(dlg.rows.keys()))
dlg.destroy()

app._apply_aliases({"Jorge": "Jorge Prado"})
print("alias saved:", app.repo.get_aliases())
ok2 = app.on_resolve()
print("resolve all ok after override (expect True):", ok2)
print("preview head:", app.preview_box.get("1.0", "end").strip().splitlines()[0])

lus, wcs = app._pending_resolved
plan = build_plan(lus, wcs, app.repo.list_accounts())
app.plan = plan
app._run_status = {}
app._persist_plan()
app._persist_status()
app._populate_run_table()
print("plan:", plan.summary())
print("run rows:", len(app.run_tree.get_children()))

# Simulate a couple of run results + a start-offset skip, then persist.
first_id = plan.assignments[0].account_id
second_id = plan.assignments[1].account_id
app._update_run_row(first_id, "Picks submitted and confirmed.", "ok")
app._update_run_row(second_id, "Failed after 2 attempts: timeout", "fail")
app._persist_status()
print("read options:", app._read_run_options())

app.update()
# Close via the real close handler (persists round text + plan + statuses).
app._on_close()

# --- Simulate an app restart: a fresh App with the same RMFANTASY_HOME. ---
app2 = App()
print("restart: plan loaded:", app2.plan is not None)
print("restart: run rows:", len(app2.run_tree.get_children()))
print("restart: lineups restored:", bool(app2.lineups_box.get("1.0", "end").strip()))
restored = app2._run_status.get(first_id)
print("restart: first account status restored:", restored)
print("restart: history rows:", len(app2.history_tree.get_children()))

# StartFromDialog: clamps to max and writes an int result (no wait_window here).
from rmfantasy.ui.app import StartFromDialog
sd = StartFromDialog(app2, max_n=8, default=1)
sd.entry.delete(0, "end")
sd.entry.insert(0, "99")
sd._ok()
print("start dialog clamp (expect 8):", sd.result)

# --- Start-at-account dropdown: pick the 5th account, rebuild, verify offset ---
app2._refresh_start_at_choices()
choices = app2.start_at_combo.all_values()
print("start-at choices count (expect 16):", len(choices))
app2.start_at_combo.set(choices[4])            # 5th account
off = app2._selected_start_offset()
print("start-at offset (expect 4):", off)
accs = app2.repo.list_accounts()
p_off = build_plan([["a", "b", "c", "d", "e"]], ["W0", "W1", "W2"], accs, off)
print("start-at first acct == 5th:", p_off.assignments[0].account_label == accs[4].label)
print("start-at skipped_before (expect 4):", p_off.skipped_before)
# Free-typed substring should still resolve to an account line.
app2.start_at_combo.set(accs[7].label)
print("start-at substring offset (expect 7):", app2._selected_start_offset())
# Run Picks within-plan skip dropdown parses the leading position number.
app2.plan = build_plan(
    [["a", "b", "c", "d", "e"], ["f", "g", "h", "i", "j"]], ["W0", "W1"], accs)
app2._populate_run_table()
app2._set_run_start_position(3)
print("run-start position parsed (expect 3):", app2._selected_run_start_position())

# --- Reset round: clears round + history, keeps accounts. ---
app2.on_reset_round()
print("after reset: plan:", app2.plan)
print("after reset: run rows:", len(app2.run_tree.get_children()))
print("after reset: accounts kept:", app2.repo.count_accounts())
print("after reset: alias kept:", app2.repo.get_aliases())

app2.update()
app2.after(50, app2.destroy)
app2.mainloop()
print("UI SMOKE OK")
