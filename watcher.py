"""Watch Facebook groups for new posts matching keywords and alert via Telegram.

Usage (ACCOUNT defaults to the first account in config.json):
  python watcher.py login [ACCOUNT]                 # visible browser; log in to Facebook once per account
  python watcher.py import-cookies FILE [ACCOUNT]   # or: import cookies exported from a logged-in browser
  python watcher.py check-proxy [ACCOUNT]           # show the public IP the account's browser uses
  python watcher.py run                             # headless polling loop over all accounts
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

import fbn_patch  # noqa: E402,F401  (skip unreadable posts instead of failing the scan)
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


def report_to_braveduck(cfg, ok, reason=""):
    """Tell the braveduck board whether this round's scan worked (`braveduck crawl-report`)."""
    bd = cfg.get("braveduck") or {}
    if not bd.get("enabled"):
        return
    args = [os.path.expanduser(bd.get("bin", "~/go/bin/braveduck"))]
    if bd.get("db"):
        args += ["--db", os.path.expanduser(bd["db"])]
    args += ["crawl-report", "facebook"] + (["--ok"] if ok else ["--fail", reason[:300]])
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass  # an older braveduck without crawl-report, or a hiccup, must never stop scanning


# --- accounts, proxies and scraping (fbn's mobile reader) ------------------

# Exit code for problems a restart can't fix (login, account check); the systemd unit doesn't restart on it.
EXIT_NEEDS_USER = 3
FATAL_ERRORS = (AuthenticationRequiredError, AccountActionRequiredError, BrowserUnavailableError, ConfigurationError)
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def parse_proxy(url):
    """Turn 'http://user:pass@host:port' (or https://, socks5://) into Playwright's proxy option."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https", "socks5") or not parts.hostname or not parts.port:
        raise ConfigurationError("proxy must look like http://[user:pass@]host:port (or https://, socks5://)")
    if parts.scheme == "socks5" and parts.username:
        raise ConfigurationError("Chromium can't use a SOCKS5 proxy with a username/password; use an http:// proxy")
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = urllib.parse.unquote(parts.username)
        proxy["password"] = urllib.parse.unquote(parts.password or "")
    return proxy


def load_accounts(cfg):
    """Accounts from config `accounts`; a config with only top-level `groups` is one account named 'default'."""
    raw = cfg.get("accounts") or [{"name": "default", "groups": cfg.get("groups", [])}]
    accounts, seen_groups = [], set()
    for entry in raw:
        name = entry.get("name", "")
        if not ACCOUNT_NAME_RE.match(name) or any(a["name"] == name for a in accounts):
            raise ConfigurationError(f"account name {name!r} must be unique, lowercase letters/digits/_/-")
        groups = []
        for g in entry.get("groups", []):
            ref = parse_group_ref(str(g["id"]))
            if ref.key in seen_groups:
                raise ConfigurationError(f"group {ref.key} is listed under more than one account")
            seen_groups.add(ref.key)
            groups.append((ref, g.get("name", ref.key)))
        accounts.append({
            "name": name,
            # 'default' keeps the original ./profile so existing logins keep working.
            "profile": PROFILE_DIR if name == "default" else ROOT / "profiles" / name,
            "proxy": parse_proxy(entry.get("proxy", "")),
            "groups": groups,
        })
    return accounts


def find_account(cfg, name):
    accounts = load_accounts(cfg)
    if name is None:
        return accounts[0]
    for account in accounts:
        if account["name"] == name:
            return account
    raise SystemExit(f"No account named {name!r} in config.json (have: {', '.join(a['name'] for a in accounts)})")


class _WithProxy:
    """Wrap a Playwright object so fbn's persistent-context launch also gets a proxy."""

    def __init__(self, playwright, proxy):
        self._playwright, self._proxy = playwright, proxy

    def __getattr__(self, name):
        return getattr(self._playwright, name)

    @property
    def chromium(self):
        chromium, proxy = self._playwright.chromium, self._proxy

        class Chromium:
            def __getattr__(self, name):
                return getattr(chromium, name)

            def launch_persistent_context(self, user_data_dir, **options):
                return chromium.launch_persistent_context(user_data_dir, proxy=proxy, **options)

        return Chromium()


class ProxiedSource(MobilePostSource):
    def __init__(self, settings, proxy=None):
        super().__init__(settings)
        self.proxy = proxy

    def _launch_context(self, playwright, **kwargs):
        if self.proxy:
            playwright = _WithProxy(playwright, self.proxy)
        return super()._launch_context(playwright, **kwargs)


def make_source(account, headless):
    return ProxiedSource(BrowserSettings(profile_dir=account["profile"], headless=headless), account["proxy"])


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


LOGIN_DONE_FILE = ROOT / ".login-done"


def wait_for_login():
    if sys.stdin.isatty():
        input("Press Enter once you are logged in... ")
        return
    # No terminal (e.g. started by another tool): wait for `touch .login-done` instead.
    print(f"Waiting for {LOGIN_DONE_FILE} to appear...", flush=True)
    while not LOGIN_DONE_FILE.exists():
        time.sleep(1)
    LOGIN_DONE_FILE.unlink()


def cmd_login(account_name=None):
    """Log in with a normal desktop window; fbn's mobile reader reuses the same profile's cookies."""
    from playwright.sync_api import sync_playwright

    account = find_account(load_config(), account_name)
    print(f"[{account['name']}] Log in to Facebook in the browser window, then come back here.", flush=True)
    LOGIN_DONE_FILE.unlink(missing_ok=True)
    with sync_playwright() as p:
        options = {"proxy": account["proxy"]} if account["proxy"] else {}
        ctx = p.chromium.launch_persistent_context(
            # WSLg ignores --start-maximized, so ask for an explicit window size.
            str(account["profile"]), headless=False, viewport={"width": 1280, "height": 860},
            args=["--window-size=1300,980", "--window-position=40,20"], **options,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/")
        wait_for_login()
        logged_in = any(c["name"] == "c_user" for c in ctx.cookies("https://www.facebook.com"))
        ctx.close()
    if not logged_in:
        print("Not logged in yet (no Facebook session cookie). Run login again.")
        raise SystemExit(EXIT_NEEDS_USER)
    print("Login saved to", account["profile"])


def cmd_import_cookies(cookie_file, account_name=None):
    """Import cookies exported from a browser that is already logged in (no login window needed)."""
    account = find_account(load_config(), account_name)
    if not account["groups"]:
        raise SystemExit(f"Account {account['name']!r} has no groups to test the login on")
    cookies, ignored = load_facebook_cookies(cookie_file)
    state = make_source(account, headless=True).bootstrap_auth(account["groups"][0][0], cookies)
    print(f"[{account['name']}] Imported {len(cookies)} Facebook cookies ({ignored} others ignored); "
          f"group page state: {state.value}")


def cmd_check_proxy(account_name=None):
    """Show the public IP Facebook would see for an account (through its proxy, if any)."""
    from playwright.sync_api import sync_playwright

    account = find_account(load_config(), account_name)
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"proxy": account["proxy"]} if account["proxy"] else {}))
        page = browser.new_page()
        page.goto("https://api.ipify.org?format=json", timeout=30000)
        ip = json.loads(page.inner_text("body"))["ip"]
        browser.close()
    via = account["proxy"]["server"] if account["proxy"] else "no proxy"
    print(f"[{account['name']}] public IP {ip} ({via})")


def cmd_run():
    cfg = load_config()
    match = build_matcher(cfg["keywords"])
    db = open_db()
    lo, hi = cfg["poll_seconds"]
    policy = ScanPolicy(sample_count=cfg["sample_count"], max_scrolls=cfg["scrolls"])
    accounts = load_accounts(cfg)
    for account in accounts:
        account["source"] = make_source(account, headless=cfg.get("headless", True))

    failing = {}  # group name -> (first failure time, consecutive failed rounds, last error)
    down = {}  # account name -> reason; its groups are skipped until you log it in again and restart
    while True:
        # Board health: ok if any group scanned this round, no group has failed 3 rounds in a row
        # and no account is logged out. fbn's messages are fixed text, never post content.
        scanned, last_error = 0, ""
        for account in accounts:
            if account["name"] in down:
                continue
            for group_ref, name in account["groups"]:
                gid = group_ref.key
                # First time we see a group, record existing posts silently instead of flooding alerts.
                bootstrap = not group_has_history(db, gid)
                try:
                    posts = scrape_group(account["source"], db, group_ref, policy)
                except FATAL_ERRORS as e:
                    down[account["name"]] = f"{type(e).__name__} on account {account['name']}"
                    send_telegram(cfg, f"⚠️ fb-watcher: account {account['name']} stopped ({type(e).__name__}): {e}\n"
                                       f"Its groups are paused. Run `python watcher.py login {account['name']}` "
                                       "(or import-cookies), then restart the service.")
                    print(f"[{account['name']}] {type(e).__name__}: {e} (account paused)")
                    break
                except FbnError as e:
                    # Timeouts, layout drift, access problems: skip this group for one round.
                    print(f"[{name}] {type(e).__name__}: {e} (will retry next round)")
                    last_error = f"{type(e).__name__}: {e}"
                    since, rounds, _ = failing.get(name, (time.strftime("%H:%M"), 0, ""))
                    failing[name] = (since, rounds + 1, type(e).__name__)
                    continue
                scanned += 1
                failing.pop(name, None)

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
                print(f"[{time.strftime('%H:%M:%S')}] [{account['name']}/{name}] {len(posts)} posts, {new} new"
                      + (" (bootstrap, no alerts)" if bootstrap else ""))
                time.sleep(random.randint(3, 8))
        problems = list(down.values()) + [
            f"{err} on {name} since {since}" for name, (since, rounds, err) in failing.items() if rounds >= 3
        ]
        if len(down) == len(accounts):
            report_to_braveduck(cfg, False, "; ".join(problems))
            raise SystemExit(EXIT_NEEDS_USER)
        if problems:
            report_to_braveduck(cfg, False, "; ".join(problems))
        else:
            report_to_braveduck(cfg, scanned > 0, last_error)
        time.sleep(random.randint(lo, hi))


def main():
    args = sys.argv[1:] or ["run"]
    cmd, rest = args[0], args[1:]
    if cmd == "login" and len(rest) <= 1:
        cmd_login(*rest)
    elif cmd == "import-cookies" and len(rest) in (1, 2):
        cmd_import_cookies(*rest)
    elif cmd == "check-proxy" and len(rest) <= 1:
        cmd_check_proxy(*rest)
    elif cmd == "run" and not rest:
        cmd_run()
    elif cmd == "test-alert":
        send_telegram(load_config(), "✅ fb-watcher test alert")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
