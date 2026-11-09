"""Records the demo in the README with Demotive (https://github.com/IamOumarIbrahim/Demotive).

usage (from this repo's folder):
    python docs/demo/make_demo.py PATH/TO/Demotive takes/demo
    cd PATH/TO/Demotive
    python -m demotive.compose PATH/TO/takes/demo demo.mp4 --gif demo.gif --accent "#58a6ff" ^
        --logo G --title "GitHub News" --subtitle "The 3 new repos growing fastest today" ^
        --outro-title "Get started" --outro-subtitle "Windows | Python 3.10+ | no installs" ^
        --outro-line "Double-click GitHub News.bat"

The app loads live from GitHub, but its cache and history go to a throw-away folder.
"""

import importlib.util
import os
import shutil
import sys
import tkinter as tk

DEMOTIVE, WORK = (os.path.abspath(a) for a in sys.argv[1:3])
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, DEMOTIVE)
os.makedirs(WORK, exist_ok=True)

spec = importlib.util.spec_from_file_location("githubnews", os.path.join(ROOT, "githubnews.pyw"))
gn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gn)

# keep the real cache and history safe, but start from a copy so History has something to show
for name in ("CACHE_FILE", "HISTORY_FILE"):
    real = getattr(gn, name)
    copy = os.path.join(WORK, os.path.basename(real))
    if os.path.exists(real):
        shutil.copy(real, copy)
    setattr(gn, name, copy)
if os.path.exists(gn.CACHE_FILE):
    os.remove(gn.CACHE_FILE)

from demotive import Director  # noqa: E402

root = tk.Tk()
app = gn.App(root)


def cards():
    return [w for w in app.body.winfo_children() if isinstance(w, tk.Frame)]


def script(d: Director):
    yield from d.caption("It checks GitHub for new repos that are growing fast")
    yield from d.speed(4)
    yield from d.until(lambda: len(cards()) == 3, timeout=120)
    yield from d.speed(1)
    yield from d.wait(0.6)

    yield from d.caption("You get 3 picks a day, never the same repo twice")
    for c in cards():
        yield from d.move(c, dx=-150)
        yield from d.wait(0.9)
    yield from d.focus(cards()[0], 1.35, pad=6)
    yield from d.wait(2.0)
    yield from d.focus(None)

    yield from d.caption("Green shows the stars it got today")
    yield from d.focus(cards()[0], 1.6, pad=4)
    yield from d.wait(2.2)
    yield from d.focus(None)

    yield from d.caption("History keeps every day's picks")
    history_btn = [w for w in app.refresh_btn.master.winfo_children()
                   if isinstance(w, tk.Button) and "History" in w.cget("text")][0]
    yield from d.move(history_btn)
    yield from d.click(app.show_history)
    win = root.winfo_children()[-1]
    win.attributes("-topmost", True)
    win.geometry(f"560x430+{root.winfo_rootx() + 150}+{root.winfo_rooty() + 40}")
    yield from d.wait(0.5)
    yield from d.move((430, 260), 0.8)
    yield from d.wait(2.5)
    win.destroy()

    yield from d.caption("It opens by itself when Windows starts")
    yield from d.move(app.refresh_btn, dx=-260)
    yield from d.wait(2.2)
    yield from d.caption(None)
    yield from d.wait(0.5)


d = Director(root, WORK, size=(860, 520), pos=(530, 220))
d.run(script)
