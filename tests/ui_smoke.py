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
app._populate_run_table()
print("plan:", plan.summary())
print("run rows:", len(app.run_tree.get_children()))
print("history rows:", len(app.history_tree.get_children()))

app.update()
app.after(50, app.destroy)
app.mainloop()
print("UI SMOKE OK")
