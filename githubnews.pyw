"""
GitHub News - shows the top 3 NEW repos gaining the most stars today.

Just double-click this file (or the "GitHub News" shortcut on your desktop).
On first run it adds itself to Windows startup, so it pops up every time
you log in. Untick the checkbox at the bottom of the window to stop that.

No installs needed: pure Python standard library.
"""

import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from html import unescape
import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------- settings
MAX_AGE_DAYS = 120         # repo must be created within this many days (~4 months)
MAX_TOTAL_STARS = 30000    # skip repos that are already huge/famous
TOP_N = 3
CACHE_HOURS = 3            # reuse fetched results for this long
LANGUAGES = ["", "python", "typescript", "javascript", "rust", "go",
             "c++", "java", "jupyter-notebook", "swift", "c%23"]

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(APP_DIR, "cache.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
META_FILE = os.path.join(APP_DIR, "repo_info_cache.json")
LOG_FILE = os.path.join(APP_DIR, "error.log")
SCRIPT = os.path.abspath(__file__)
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup", "GitHub News.lnk")
SETUP_FLAG = os.path.join(APP_DIR, ".setup_done")

UA = {"User-Agent": "githubnews-desktop-app", "Accept": "application/vnd.github+json"}

# ---------------------------------------------------------------- colors
BG = "#0d1117"
CARD = "#161b22"
BORDER = "#30363d"
FG = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
HOT = "#ff7b39"
GREEN = "#3fb950"


def log_error(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


_TOKEN = None


def github_token():
    """Use GITHUB_TOKEN or your `gh` login if present (much higher rate limit).
    The token is only ever sent to api.github.com."""
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = os.environ.get("GITHUB_TOKEN", "")
        if not _TOKEN:
            try:
                out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                     timeout=10, creationflags=0x08000000 if os.name == "nt" else 0)
                _TOKEN = out.stdout.strip() if out.returncode == 0 else ""
            except Exception:
                _TOKEN = ""
    return _TOKEN


def http_get(url, timeout=20):
    headers = dict(UA)
    if url.startswith("https://api.github.com/") and github_token():
        headers["Authorization"] = f"Bearer {github_token()}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- data
def scrape_trending(lang=""):
    """Returns list of dicts from github.com/trending (daily)."""
    url = f"https://github.com/trending/{lang}?since=daily" if lang else "https://github.com/trending?since=daily"
    html = http_get(url)
    out = []
    for block in html.split('<article class="Box-row"')[1:]:
        m = re.search(r'<h2 class="h3 lh-condensed">.*?href="/([^"/]+/[^"/]+)"', block, re.S)
        if not m:
            continue
        today = re.search(r'([\d,]+)\s+stars? today', block)
        desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', block, re.S)
        lang_m = re.search(r'itemprop="programmingLanguage">([^<]+)<', block)
        out.append({
            "name": m.group(1).strip(),
            "today": int(today.group(1).replace(",", "")) if today else 0,
            "desc": unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
            "lang": lang_m.group(1).strip() if lang_m else "",
        })
    return out


def age_days(created_iso):
    created = datetime.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - created).days)


def load_meta():
    try:
        with open(META_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_meta(meta):
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError as e:
        log_error(f"meta save failed: {e}")


def meta_fresh(m):
    # old repos stay old forever; new ones get their star count re-checked every 12 h
    return age_days(m["created_at"]) > MAX_AGE_DAYS or time.time() - m.get("fetched", 0) < 12 * 3600


def search_batch(names, meta):
    """Look up several repos in ONE search request (query must stay short)."""
    q = " ".join(f"repo:{n}" for n in names)
    url = "https://api.github.com/search/repositories?per_page=100&q=" + urllib.parse.quote(q)
    for it in json.loads(http_get(url)).get("items", []):
        meta[it["full_name"].lower()] = {
            "full_name": it["full_name"], "created_at": it["created_at"],
            "stargazers_count": it["stargazers_count"], "html_url": it["html_url"],
            "description": it.get("description") or "", "language": it.get("language") or "",
            "fetched": time.time()}


def fetch_top(exclude=()):
    exclude = {e.lower() for e in exclude}
    # 1) collect today's trending from several pages
    cands = {}

    def safe_scrape(lang):
        try:
            return scrape_trending(lang)
        except Exception as e:
            log_error(f"trending {lang or 'all'} failed: {e}")
            return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(safe_scrape, LANGUAGES))
    for page in pages:
        for r in page:
            key = r["name"].lower()
            if key not in cands or r["today"] > cands[key]["today"]:
                cands[key] = r
    if not cands:
        raise RuntimeError("Couldn't reach GitHub. Are you online?")

    # 2) walk candidates from most stars today down; look up only what we need
    meta = load_meta()
    order = sorted((k for k in cands if k not in exclude), key=lambda k: cands[k]["today"], reverse=True)
    picks, calls, max_calls = [], 0, (25 if github_token() else 8)

    def make_pick(key):
        m, r = meta[key], cands[key]
        if age_days(m["created_at"]) > MAX_AGE_DAYS or m["stargazers_count"] > MAX_TOTAL_STARS:
            return None
        return {**r, "name": m["full_name"], "desc": r["desc"] or m["description"],
                "lang": r["lang"] or m["language"], "stars": m["stargazers_count"],
                "age": age_days(m["created_at"]), "url": m["html_url"], "source": "trending"}

    i = 0
    while i < len(order) and len(picks) < TOP_N:
        key = order[i]
        if key not in meta or not meta_fresh(meta[key]):
            # batch this one with the next few unknown ones (query limit ~256 chars)
            batch, length = [], 0
            for k in order[i:]:
                if k in meta and meta_fresh(meta[k]):
                    continue
                if length + len(k) + 6 > 240:
                    break
                batch.append(cands[k]["name"])
                length += len(k) + 6
            if calls >= max_calls:
                break
            try:
                search_batch(batch, meta)
            except Exception as e:
                log_error(f"lookup failed: {e}")
                break
            calls += 1
            for n in batch:  # don't retry repos the search didn't return
                meta.setdefault(n.lower(), {"full_name": n, "created_at": "2000-01-01T00:00:00Z",
                                            "stargazers_count": 0, "html_url": f"https://github.com/{n}",
                                            "description": "", "language": "", "fetched": time.time()})
        p = make_pick(key)
        if p:
            picks.append(p)
        i += 1
    save_meta(meta)

    # 3) fallback: brand-new repos (last 7 days) with the most stars
    if len(picks) < TOP_N:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=30&q="
               + urllib.parse.quote(f"created:>={since}"))
        try:
            have = {p["name"].lower() for p in picks} | exclude
            for m in json.loads(http_get(url)).get("items", []):
                if len(picks) >= TOP_N:
                    break
                if m["full_name"].lower() in have:
                    continue
                picks.append({"name": m["full_name"], "today": None,
                              "desc": m.get("description") or "", "lang": m.get("language") or "",
                              "stars": m["stargazers_count"], "age": age_days(m["created_at"]),
                              "url": m["html_url"], "source": "new"})
        except Exception as e:
            log_error(f"fallback search failed: {e}")
    return picks


# ---------------------------------------------------------------- history
def today_key():
    return datetime.now().strftime("%Y-%m-%d")


def load_history():
    """{"2026-11-09": [repo, repo, repo], ...}"""
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(hist):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2)
    except OSError as e:
        log_error(f"history save failed: {e}")


def past_names(hist):
    """Every repo recommended on a previous day (so it never repeats)."""
    return {r["name"] for day, repos in hist.items() if day != today_key() for r in repos}


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(repos):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "repos": repos}, f, indent=2)
    except OSError as e:
        log_error(f"cache save failed: {e}")


# ---------------------------------------------------------------- shortcuts
def pythonw():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def make_shortcut(lnk_path):
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TGT;$s.Arguments='\"'+$env:SCR+'\"';"
        "$s.WorkingDirectory=$env:WD;$s.Description='GitHub News';$s.Save()"
    )
    env = {**os.environ, "LNK": lnk_path, "TGT": pythonw(), "SCR": SCRIPT, "WD": APP_DIR}
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                   creationflags=0x08000000, check=False)  # CREATE_NO_WINDOW


def desktop_dir():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True, creationflags=0x08000000)
        d = out.stdout.strip()
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def first_run_setup():
    """Lazy-proof: auto-add to startup + desktop shortcut the first time."""
    if os.path.exists(SETUP_FLAG) or os.name != "nt":
        return
    try:
        make_shortcut(STARTUP_LNK)
        make_shortcut(os.path.join(desktop_dir(), "GitHub News.lnk"))
        open(SETUP_FLAG, "w").close()
    except Exception as e:
        log_error(f"setup failed: {e}")


def set_startup(enabled):
    try:
        if enabled:
            make_shortcut(STARTUP_LNK)
        elif os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
    except Exception as e:
        log_error(f"startup toggle failed: {e}")


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title("GitHub News")
        root.configure(bg=BG)
        root.geometry("760x640")
        root.minsize(560, 520)

        self.f_title = tkfont.Font(family="Segoe UI", size=18, weight="bold")
        self.f_sub = tkfont.Font(family="Segoe UI", size=10)
        self.f_name = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        self.f_body = tkfont.Font(family="Segoe UI", size=10)
        self.f_rank = tkfont.Font(family="Segoe UI", size=26, weight="bold")
        self.f_chip = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=20, pady=(18, 4))
        tk.Label(head, text="\U0001F525 GitHub News", font=self.f_title, bg=BG, fg=FG).pack(anchor="w")
        tk.Label(head, text=f"New repos blowing up today  •  {datetime.now():%A, %B %d}",
                 font=self.f_sub, bg=BG, fg=MUTED).pack(anchor="w")

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=8)

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=20, pady=(0, 14))
        self.status = tk.Label(foot, text="", font=self.f_sub, bg=BG, fg=MUTED)
        self.status.pack(side="left")
        self.refresh_btn = tk.Button(foot, text="↻  Refresh", command=lambda: self.load(force=True),
                                     bg=CARD, fg=FG, activebackground=BORDER, activeforeground=FG,
                                     relief="flat", padx=12, pady=4, cursor="hand2", font=self.f_sub)
        self.refresh_btn.pack(side="right")
        tk.Button(foot, text="📅  History", command=self.show_history,
                  bg=CARD, fg=FG, activebackground=BORDER, activeforeground=FG,
                  relief="flat", padx=12, pady=4, cursor="hand2", font=self.f_sub).pack(side="right", padx=(0, 8))
        self.startup_var = tk.BooleanVar(value=os.path.exists(STARTUP_LNK))
        tk.Checkbutton(foot, text="Open when Windows starts", variable=self.startup_var,
                       command=lambda: set_startup(self.startup_var.get()),
                       bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=self.f_sub, cursor="hand2").pack(side="right", padx=12)

        root.bind("<F5>", lambda e: self.load(force=True))
        root.bind("<Escape>", lambda e: root.destroy())
        self.load()

    # -- loading
    def message(self, text, color=MUTED):
        for w in self.body.winfo_children():
            w.destroy()
        tk.Label(self.body, text=text, font=self.f_name, bg=BG, fg=color, wraplength=600,
                 justify="center").pack(expand=True)

    def load(self, force=False):
        cache = load_cache()
        fresh = cache and time.time() - cache.get("time", 0) < CACHE_HOURS * 3600             and datetime.fromtimestamp(cache["time"]).strftime("%Y-%m-%d") == today_key()
        if not force and fresh:
            self.show(cache["repos"], cache["time"])
            return
        self.refresh_btn.config(state="disabled")
        self.message("Checking what's hot on GitHub…")
        self.status.config(text="Loading…")
        threading.Thread(target=self._worker, args=(cache,), daemon=True).start()

    def _worker(self, cache):
        try:
            hist = load_history()
            repos = fetch_top(exclude=past_names(hist))
            if not repos:
                raise RuntimeError("No new hot repos found right now.")
            hist[today_key()] = repos
            save_history(hist)
            save_cache(repos)
            self.root.after(0, self.show, repos, time.time())
        except Exception as e:
            log_error(traceback.format_exc())
            self.root.after(0, self.fail, str(e), cache)

    def fail(self, err, cache):
        self.refresh_btn.config(state="normal")
        if cache and cache.get("repos"):
            self.show(cache["repos"], cache["time"], stale=True)
        else:
            self.message(f"Couldn't load repos:\n{err}\n\nPress Refresh to try again.", HOT)
            self.status.config(text="")

    # -- rendering
    def show(self, repos, ts, stale=False):
        self.refresh_btn.config(state="normal")
        for w in self.body.winfo_children():
            w.destroy()
        for i, r in enumerate(repos, 1):
            self.card(i, r)
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        self.status.config(text=("Offline — showing saved results from " if stale else "Updated ") + when,
                           fg=HOT if stale else MUTED)

    def show_history(self):
        hist = load_history()
        win = tk.Toplevel(self.root, bg=BG)
        win.title("GitHub News — History")
        win.geometry("620x600")
        tk.Label(win, text=f"📅 Past picks ({len(hist)} day{'' if len(hist) == 1 else 's'})", font=self.f_title,
                 bg=BG, fg=FG).pack(anchor="w", padx=16, pady=(14, 6))
        txt = tk.Text(win, bg=CARD, fg=FG, font=self.f_body, relief="flat", wrap="word",
                      padx=12, pady=10, insertbackground=FG, cursor="arrow")
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=(0, 14))
        txt.pack(fill="both", expand=True, padx=(16, 0), pady=(0, 14))
        txt.tag_config("day", font=self.f_name, foreground=HOT, spacing1=10)
        txt.tag_config("muted", foreground=MUTED)
        if not hist:
            txt.insert("end", "Nothing yet — come back tomorrow!", "muted")
        for day in sorted(hist, reverse=True):
            label = datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %d %Y")
            txt.insert("end", label + "\n", "day")
            for i, r in enumerate(hist[day], 1):
                tag = f"link{day}{i}"
                txt.tag_config(tag, foreground=ACCENT, underline=True)
                txt.tag_bind(tag, "<Button-1>", lambda e, u=r["url"]: webbrowser.open(u))
                txt.tag_bind(tag, "<Enter>", lambda e: txt.config(cursor="hand2"))
                txt.tag_bind(tag, "<Leave>", lambda e: txt.config(cursor="arrow"))
                txt.insert("end", f"  {i}. ")
                txt.insert("end", r["name"], tag)
                gain = f"+{r['today']:,} today" if r.get("today") is not None else "new this week"
                txt.insert("end", f"   ({gain}, ★ {r['stars']:,})\n", "muted")
                if r.get("desc"):
                    txt.insert("end", f"     {r['desc']}\n", "muted")
        txt.config(state="disabled")

    def chip(self, parent, text, color):
        tk.Label(parent, text=text, font=self.f_chip, bg=BG, fg=color, padx=8, pady=2).pack(side="left", padx=(0, 6))

    def card(self, rank, r):
        outer = tk.Frame(self.body, bg=BORDER)
        outer.pack(fill="x", pady=6)
        c = tk.Frame(outer, bg=CARD, cursor="hand2")
        c.pack(fill="both", padx=1, pady=1)

        tk.Label(c, text=f"#{rank}", font=self.f_rank, bg=CARD, fg=HOT if rank == 1 else ACCENT,
                 width=3).pack(side="left", padx=(10, 4), pady=10, anchor="n")
        col = tk.Frame(c, bg=CARD)
        col.pack(side="left", fill="both", expand=True, pady=10, padx=(0, 14))

        name = tk.Label(col, text=r["name"], font=self.f_name, bg=CARD, fg=ACCENT, cursor="hand2", anchor="w")
        name.pack(fill="x")
        desc = tk.Label(col, text=r["desc"] or "(no description)", font=self.f_body, bg=CARD, fg=FG,
                        wraplength=560, justify="left", anchor="w")
        desc.pack(fill="x", pady=(2, 8))

        chips = tk.Frame(col, bg=CARD)
        chips.pack(fill="x")
        if r.get("today") is not None:
            self.chip(chips, f"▲ +{r['today']:,} stars today", GREEN)
        else:
            self.chip(chips, "✨ brand new this week", GREEN)
        self.chip(chips, f"★ {r['stars']:,} total", FG)
        age = r["age"]
        self.chip(chips, "created today" if age == 0 else f"created {age} day{'s' if age != 1 else ''} ago", MUTED)
        if r.get("lang"):
            self.chip(chips, r["lang"], MUTED)

        def open_repo(_e=None, url=r["url"]):
            webbrowser.open(url)
        for w in (c, col, name, desc, chips):
            w.bind("<Button-1>", open_repo)

        def resize(e, d=desc):
            d.config(wraplength=max(200, e.width - 10))
        col.bind("<Configure>", resize)


def main():
    first_run_setup()
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.lift()
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
