"""Watch Facebook groups for new posts matching keywords and alert via Telegram.

Usage:
  python watcher.py login                     # opens a visible browser; log in to Facebook once
  python watcher.py import-cookies FILE       # or: import cookies exported from a logged-in browser
  python watcher.py run       # headless polling loop
  python watcher.py test-alert
"""

import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "profile"
DB_PATH = ROOT / "seen.db"
CONFIG_PATH = ROOT / "config.json"

# Chromium system libs unpacked without root (see README); the browser process inherits this.
LOCAL_LIBS = ROOT / "libs" / "root" / "usr" / "lib" / "x86_64-linux-gnu"
if LOCAL_LIBS.is_dir():
    os.environ["LD_LIBRARY_PATH"] = f"{LOCAL_LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"

from fbn.auth import load_facebook_cookies  # noqa: E402
from fbn.config import BrowserSettings  # noqa: E402
from fbn.exceptions import (  # noqa: E402
    AccountActionRequiredError,
    AuthenticationRequiredError,
    BrowserUnavailableError,
    ConfigurationError,
    FbnError,
)
from fbn.extractor import parse_group_ref  # noqa: E402
from fbn.mobile import MobilePostSource  # noqa: E402
from fbn.models import ScanPolicy  # noqa: E402


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg.setdefault("poll_seconds", [90, 180])
    cfg.setdefault("scrolls", 4)
    cfg.setdefault("sample_count", 10)
    cfg["telegram_token"] = os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegram_token", "")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
    return cfg


# --- keyword matching -------------------------------------------------------

def normalize(text):
    """Lowercase and strip accents so 'Tự động hóa' matches 'tu dong hoa'."""
    text = text.lower().replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def build_matcher(keywords):
    # A trailing '*' makes a prefix match: 'automat*' matches automate/automated/automation.
    patterns = [
        re.compile(r"(?<!\w)" + re.escape(normalize(k.rstrip("*"))) + ("" if k.endswith("*") else r"(?!\w)"))
        for k in keywords
    ]

    def match(text):
        norm = normalize(text)
        return [k for k, p in zip(keywords, patterns) if p.search(norm)]

    return match


# --- storage ----------------------------------------------------------------

def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (post_key TEXT PRIMARY KEY, group_id TEXT, ts INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS boundary (group_id TEXT PRIMARY KEY, post_id TEXT)")
    return db


def is_seen(db, key):
    return db.execute("SELECT 1 FROM seen WHERE post_key = ?", (key,)).fetchone() is not None


def mark_seen(db, key, group_id):
    db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (key, group_id, int(time.time())))
    db.commit()


def group_has_history(db, group_id):
    return db.execute("SELECT 1 FROM seen WHERE group_id = ? LIMIT 1", (group_id,)).fetchone() is not None


# --- alerts -----------------------------------------------------------------

def send_telegram(cfg, text):
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        print("[alert]", text)
        return
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": cfg["telegram_chat_id"],
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=15).read()
    except Exception as e:
        print("telegram send failed:", e)


def add_to_braveduck(cfg, post, group_id, group_name, hits):
    """Add a matched post to the braveduck job board via `braveduck add`."""
    bd = cfg.get("braveduck") or {}
    if not bd.get("enabled"):
        return
    first_line = next((line.strip() for line in post["text"].splitlines() if line.strip()), "Facebook post")
    title = first_line if len(first_line) <= 120 else first_line[:117] + "..."
    group_url = f"https://www.facebook.com/groups/{group_id}/"
    jd = (
        f"{post['text']}\n\n---\n"
        f"Author: {post['author'] or 'unknown'}\n"
        f"Group: {group_name} ({group_url})\n"
        f"Matched keywords: {', '.join(hits)}"
    )
    args = [os.path.expanduser(bd.get("bin", "~/go/bin/braveduck"))]
    if bd.get("db"):
        args += ["--db", os.path.expanduser(bd["db"])]
    # Only a real permalink goes in --url: braveduck dedups on it, so a shared group URL would collide.
    args += ["add", "--title", title, "--company", group_name, "--source", "facebook", "--jd", jd]
    if post["url"]:
        args += ["--url", post["url"]]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("braveduck add failed:", e)
        return
    out = (res.stdout or res.stderr).strip()
    if res.returncode == 0 or "already on the board" in out:
        print("[braveduck]", out)
    else:
        print("braveduck add failed:", out)


# --- scraping (fbn's mobile reader) ----------------------------------------

# Exit code for problems a restart can't fix (login, account check); the systemd unit doesn't restart on it.
EXIT_NEEDS_USER = 3
FATAL_ERRORS = (AuthenticationRequiredError, AccountActionRequiredError, BrowserUnavailableError, ConfigurationError)


def make_source(headless):
    return MobilePostSource(BrowserSettings(profile_dir=PROFILE_DIR, headless=headless))


def get_boundary(db, group_id):
    row = db.execute("SELECT post_id FROM boundary WHERE group_id = ?", (group_id,)).fetchone()
    return row[0] if row else None


def set_boundary(db, group_id, post_id):
    db.execute("INSERT OR REPLACE INTO boundary VALUES (?, ?)", (group_id, post_id))
    db.commit()


def scrape_group(source, db, group_ref, policy):
    """Return the group's recent posts, newest first, scrolling back to the last scan's newest post."""
    scan = source.fetch_recent(group_ref, policy, boundary_post_id=get_boundary(db, group_ref.key))
    posts = sorted(scan.posts, key=lambda post: post.position)
    if posts:
        set_boundary(db, group_ref.key, posts[0].post_id)
    return [
        {"key": f"{group_ref.key}:{post.post_id}", "text": post.text, "url": post.url, "author": post.author or ""}
        for post in posts
    ]


def cmd_login():
    print("Log in to Facebook in the browser window, then come back here.")
    make_source(headless=False).interactive_login(lambda: input("Press Enter once you are logged in... "))
    print("Login verified and saved to", PROFILE_DIR)


def cmd_import_cookies(cookie_file):
    """Import cookies exported from a browser that is already logged in (no login window needed)."""
    cfg = load_config()
    cookies, ignored = load_facebook_cookies(cookie_file)
    group_ref = parse_group_ref(str(cfg["groups"][0]["id"]))
    state = make_source(headless=True).bootstrap_auth(group_ref, cookies)
    print(f"Imported {len(cookies)} Facebook cookies ({ignored} others ignored); group page state: {state.value}")


def cmd_run():
    cfg = load_config()
    match = build_matcher(cfg["keywords"])
    db = open_db()
    lo, hi = cfg["poll_seconds"]
    source = make_source(headless=cfg.get("headless", True))
    policy = ScanPolicy(sample_count=cfg["sample_count"], max_scrolls=cfg["scrolls"])
    groups = [(parse_group_ref(str(g["id"])), g.get("name", str(g["id"]))) for g in cfg["groups"]]

    while True:
        for group_ref, name in groups:
            gid = group_ref.key
            # First time we see a group, record existing posts silently instead of flooding alerts.
            bootstrap = not group_has_history(db, gid)
            try:
                posts = scrape_group(source, db, group_ref, policy)
            except FATAL_ERRORS as e:
                send_telegram(cfg, f"⚠️ fb-watcher stopped ({type(e).__name__}): {e}\n"
                                   "Run `python watcher.py login` (or import-cookies) again.")
                raise SystemExit(EXIT_NEEDS_USER)
            except FbnError as e:
                # Timeouts, layout drift, access problems: skip this group for one round.
                print(f"[{name}] {type(e).__name__}: {e} (will retry next round)")
                continue

            new = 0
            for post in posts:
                if is_seen(db, post["key"]):
                    continue
                mark_seen(db, post["key"], gid)
                new += 1
                if bootstrap:
                    continue
                hits = match(post["text"])
                if hits:
                    msg = (
                        f"🔔 [{name}] {post['author']}\n"
                        f"Keywords: {', '.join(hits)}\n\n"
                        f"{post['text'][:1500]}\n\n"
                        f"{post['url']}"
                    )
                    send_telegram(cfg, msg)
                    add_to_braveduck(cfg, post, gid, name, hits)
            print(f"[{time.strftime('%H:%M:%S')}] [{name}] {len(posts)} posts, {new} new"
                  + (" (bootstrap, no alerts)" if bootstrap else ""))
            time.sleep(random.randint(3, 8))
        time.sleep(random.randint(lo, hi))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "login":
        cmd_login()
    elif cmd == "import-cookies" and len(sys.argv) == 3:
        cmd_import_cookies(sys.argv[2])
    elif cmd == "run":
        cmd_run()
    elif cmd == "test-alert":
        send_telegram(load_config(), "✅ fb-watcher test alert")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
