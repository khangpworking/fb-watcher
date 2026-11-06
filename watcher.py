"""Watch Facebook groups for new posts matching keywords and alert via Telegram.

Usage (ACCOUNT defaults to the first account in config.json):
  python watcher.py login [ACCOUNT]                 # visible browser; log in to Facebook once per account
  python watcher.py import-cookies FILE [ACCOUNT]   # or: import cookies exported from a logged-in browser
  python watcher.py check-proxy [ACCOUNT]           # show the public IP the account's browser uses
  python watcher.py run                             # headless polling loop over all accounts
  python watcher.py fetch-post URL [ACCOUNT] [--json] [--assets-dir DIR]   # read one post (README)
  python watcher.py test-alert
  python watcher.py serve-braveduck                 # scan the groups braveduck users follow (braveduck_service)
  python watcher.py check-group LINK [ACCOUNT]      # show what serve-braveduck sees on a group's page
"""

import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
# FB_WATCHER_DATA keeps the login profiles, seen.db and config.json apart from
# the code (a container's volume); by default they sit next to watcher.py.
DATA = Path(os.environ.get("FB_WATCHER_DATA") or ROOT)
PROFILE_DIR = DATA / "profile"
DB_PATH = DATA / "seen.db"
CONFIG_PATH = DATA / "config.json"

# Chromium system libs unpacked without root (see README); the browser process inherits this.
LOCAL_LIBS = ROOT / "libs" / "root" / "usr" / "lib" / "x86_64-linux-gnu"
if LOCAL_LIBS.is_dir():
    os.environ["LD_LIBRARY_PATH"] = f"{LOCAL_LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"

import fbn_patch  # noqa: E402  (fixes to fbn; see the top of fbn_patch.py)
from fbn import exceptions as fbn_exceptions  # noqa: E402
from fbn.auth import load_facebook_cookies  # noqa: E402
from fbn.config import BrowserSettings  # noqa: E402
from fbn.exceptions import (  # noqa: E402
    AccountActionRequiredError,
    AuthenticationRequiredError,
    BrowserUnavailableError,
    ConfigurationError,
    FbnError,
    ProfileInUseError,
)
from fbn.extractor import parse_group_ref  # noqa: E402
from fbn.mobile import MobilePostSource  # noqa: E402
from fbn.models import ScanPolicy  # noqa: E402


def load_config():
    # A container in braveduck's compose may run on environment settings alone.
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
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
    for k in keywords:
        if not normalize(k.rstrip("*")).strip():
            raise ConfigurationError(f"keyword {k!r} is empty; remove it from config.json")
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
    # A group is initialized after its first successful scan, even one that found no posts.
    db.execute("CREATE TABLE IF NOT EXISTS group_state (group_id TEXT PRIMARY KEY, initialized INTEGER NOT NULL)")
    db.execute("INSERT OR IGNORE INTO group_state (group_id, initialized) SELECT DISTINCT group_id, 1 FROM seen")
    if "incomplete" not in [row[1] for row in db.execute("PRAGMA table_info(group_state)")]:
        # Consecutive scans that didn't reach the stored boundary.
        db.execute("ALTER TABLE group_state ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0")
    # Fingerprint (author + opening text) -> link of every post seen, so scans can skip
    # re-visiting posts already handled (see fbn_patch.known_links).
    db.execute("""CREATE TABLE IF NOT EXISTS post_cache (
        group_id TEXT, fingerprint TEXT, url TEXT NOT NULL, ts INTEGER NOT NULL,
        PRIMARY KEY (group_id, fingerprint))""")
    # Deliveries still owed for matched posts: one row per post and sink, deleted once delivered.
    db.execute("""CREATE TABLE IF NOT EXISTS outbox (
        post_key TEXT, sink TEXT, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        created INTEGER NOT NULL, PRIMARY KEY (post_key, sink))""")
    db.commit()
    return db


def is_seen(db, key):
    return db.execute("SELECT 1 FROM seen WHERE post_key = ?", (key,)).fetchone() is not None


def mark_seen(db, key, group_id):
    """Record a post as handled. Callers commit, together with any outbox rows for it."""
    db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (key, group_id, int(time.time())))


def enqueue(db, key, sink, payload):
    db.execute("INSERT OR IGNORE INTO outbox (post_key, sink, payload, created) VALUES (?, ?, ?, ?)",
               (key, sink, json.dumps(payload, ensure_ascii=False), int(time.time())))


def group_initialized(db, group_id):
    return db.execute("SELECT 1 FROM group_state WHERE group_id = ? AND initialized", (group_id,)).fetchone() is not None


def mark_group_initialized(db, group_id):
    db.execute("INSERT OR IGNORE INTO group_state (group_id, initialized) VALUES (?, 1)", (group_id,))
    db.execute("UPDATE group_state SET initialized = 1 WHERE group_id = ?", (group_id,))
    db.commit()


def bump_incomplete(db, group_id, reset=False):
    """Count consecutive scans that didn't reach the boundary; return the new count."""
    db.execute("INSERT OR IGNORE INTO group_state (group_id, initialized) VALUES (?, 1)", (group_id,))
    db.execute("UPDATE group_state SET incomplete = CASE WHEN ? THEN 0 ELSE incomplete + 1 END WHERE group_id = ?",
               (reset, group_id))
    db.commit()
    return db.execute("SELECT incomplete FROM group_state WHERE group_id = ?", (group_id,)).fetchone()[0]


# --- alerts -----------------------------------------------------------------

def send_telegram(cfg, text):
    """Send a Telegram message; True if delivered (or Telegram isn't configured and it was printed)."""
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        print("[alert]", text)
        return True
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": cfg["telegram_chat_id"],
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=15).read()
        return True
    except Exception as e:
        print("telegram send failed:", type(e).__name__)
        return False


MAX_JD_BYTES = 100_000


def add_to_braveduck(cfg, post, group_id, group_name, hits):
    """Add a matched post to the braveduck job board; True if it's on the board (new or already there)."""
    bd = cfg.get("braveduck") or {}
    if not bd.get("enabled"):
        return True
    first_line = next((line.strip() for line in post["text"].splitlines() if line.strip()), "Facebook post")
    title = first_line if len(first_line) <= 120 else first_line[:117] + "..."
    group_url = f"https://www.facebook.com/groups/{group_id}/"
    text = post["text"]
    if len(text.encode()) > MAX_JD_BYTES:
        # One command-line argument is limited to 128 KB; say so instead of failing the add.
        text = text.encode()[:MAX_JD_BYTES].decode(errors="ignore") + "\n\n[Post cut at 100 KB; full text at the link.]"
    jd = (
        f"{text}\n\n---\n"
        f"Author: {post.get('author') or 'unknown'}\n"
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
        print("braveduck add failed:", type(e).__name__)
        return False
    out = (res.stdout or res.stderr).strip()
    if res.returncode == 0 or "already on the board" in out:
        print("[braveduck]", out)
        return True
    print("braveduck add failed:", out[:300])
    return False


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
ACCOUNT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def parse_proxy(url):
    """Turn 'http://user:pass@host:port' (or https://, socks5://) into Playwright's proxy option."""
    if not url:
        return None
    bad = ConfigurationError("proxy must look like http://[user:pass@]host:port (or https://, socks5://)")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise bad from None
    if parts.scheme not in ("http", "https", "socks5") or not parts.hostname or not port:
        raise bad
    if parts.scheme == "socks5" and parts.username:
        raise ConfigurationError("Chromium can't use a SOCKS5 proxy with a username/password; use an http:// proxy")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    proxy = {"server": f"{parts.scheme}://{host}:{port}"}
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
        if not isinstance(name, str) or not ACCOUNT_NAME_RE.fullmatch(name) or any(a["name"] == name for a in accounts):
            raise ConfigurationError(f"account name {name!r} must be unique, lowercase letters/digits/_/-")
        groups = []
        for g in entry.get("groups", []):
            ref = parse_group_ref(str(g["id"]))
            if not ref.key.isdecimal():
                # Post links come from /story.php?id=<numeric group>; a slug can't be checked against it.
                raise ConfigurationError(f"group {ref.key!r}: use the group's numeric ID, not its name")
            if ref.key in seen_groups:
                raise ConfigurationError(f"group {ref.key} is listed under more than one account")
            seen_groups.add(ref.key)
            groups.append((ref, g.get("name", ref.key)))
        accounts.append({
            "name": name,
            # 'default' keeps the original ./profile so existing logins keep working.
            "profile": PROFILE_DIR if name == "default" else DATA / "profiles" / name,
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


# Each scan runs in its own process group, so a hung browser can be killed with all its
# children instead of freezing every other group (a stalled navigation can block fbn's
# cleanup with no timeout of its own).
SCAN_WALL_SECONDS = 240
PROFILE_WAIT_SECONDS = 90
INCOMPLETE_ROUNDS_BEFORE_GAP = 3


class ScanWorkerError(FbnError):
    """The scan worker crashed, hung, or returned something unreadable."""


def known_links(db, group_id, limit=1000):
    rows = db.execute("SELECT fingerprint, url FROM post_cache WHERE group_id = ? ORDER BY ts DESC LIMIT ?",
                      (group_id, limit)).fetchall()
    return dict(rows)


def scan_in_process(account, group_ref, policy, boundary, headless=True, known=None):
    """Scan one group; return (posts newest first, complete) where complete means the
    scan reached `boundary` (or there was none yet). `known`: fingerprint -> URL of posts
    already handled, which the scan doesn't re-visit."""
    fbn_patch.known_links.clear()
    fbn_patch.known_links.update(known or {})
    fbn_patch.known_urls.clear()
    fbn_patch.known_urls.update(fbn_patch.known_links.values())
    give_up = time.monotonic() + PROFILE_WAIT_SECONDS
    while True:
        try:
            scan = make_source(account, headless).fetch_recent(group_ref, policy, boundary_post_id=boundary)
            break
        except ProfileInUseError:
            # Another tool (e.g. `watcher.py fetch-post`) is using this account's browser.
            if time.monotonic() >= give_up:
                raise
            time.sleep(3)
    posts = sorted(scan.posts, key=lambda post: post.position)
    complete = boundary is None or any(post.post_id == boundary for post in posts)
    return [
        {"post_id": post.post_id, "key": f"{group_ref.key}:{post.post_id}", "text": post.text,
         "url": post.url, "author": post.author or ""}
        for post in posts
    ], complete


def scan_in_subprocess(account, group_ref, policy, boundary, headless=True, known=None):
    request = {"known": known or {},
        "profile": str(account["profile"]), "proxy": account["proxy"], "group": group_ref.key,
        "sample_count": policy.sample_count, "max_scrolls": policy.max_scrolls,
        "boundary": boundary, "headless": headless,
    }
    worker = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "watcher.py"), "scan-worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, _ = worker.communicate(json.dumps(request), timeout=SCAN_WALL_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(worker.pid, signal.SIGKILL)  # the worker and every Chromium process it started
        worker.wait()
        raise ScanWorkerError(f"scan took longer than {SCAN_WALL_SECONDS}s and was stopped") from None
    try:
        reply = json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise ScanWorkerError(f"scan worker exited with code {worker.returncode} and no result") from None
    if reply["ok"]:
        return reply["posts"], reply["complete"]
    error = getattr(fbn_exceptions, reply["error"], None)
    if not (isinstance(error, type) and issubclass(error, FbnError)):
        error = ScanWorkerError
    raise error(reply["message"])


# Tests set FB_WATCHER_INPROCESS=1 to scan with fakes in the same process.
run_scan = scan_in_process if os.environ.get("FB_WATCHER_INPROCESS") else scan_in_subprocess


def cmd_scan_worker():
    """Internal: run one scan described on stdin; print one JSON line with the result."""
    request = json.loads(sys.stdin.read())
    account = {"profile": Path(request["profile"]), "proxy": request["proxy"]}
    policy = ScanPolicy(sample_count=request["sample_count"], max_scrolls=request["max_scrolls"])
    try:
        posts, complete = scan_in_process(
            account, parse_group_ref(request["group"]), policy, request["boundary"], request["headless"],
            request.get("known"))
        reply = {"ok": True, "posts": posts, "complete": complete}
    except Exception as e:  # sent back to the parent, which raises it there
        reply = {"ok": False, "error": type(e).__name__, "message": str(e)[:500]}
    print(json.dumps(reply, ensure_ascii=False), flush=True)


def advance_boundary(db, group_id, posts, complete):
    """Move the boundary to the newest post only when the scan reached the old one, so a
    gap of unread posts can't be skipped. After a few incomplete rounds in a row (the old
    boundary post was deleted, or one post keeps cutting the scan short), move anyway and
    return the gap size in rounds so the caller can report it."""
    if not posts:
        return 0
    if complete:
        bump_incomplete(db, group_id, reset=True)
        set_boundary(db, group_id, posts[0]["post_id"])
        return 0
    rounds = bump_incomplete(db, group_id)
    if rounds >= INCOMPLETE_ROUNDS_BEFORE_GAP:
        set_boundary(db, group_id, posts[0]["post_id"])
    return rounds


LOGIN_DONE_FILE = DATA / ".login-done"


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


def deliver_pending(cfg, db):
    """Try every owed delivery once; return (pending, failing) counts for board health.
    `failing` counts deliveries that have already failed 3 times or more."""
    rows = db.execute("SELECT post_key, sink, payload, attempts FROM outbox ORDER BY created").fetchall()
    for key, sink, payload, attempts in rows:
        item = json.loads(payload)
        if sink == "braveduck":
            ok = add_to_braveduck(cfg, item["post"], item["group_id"], item["group_name"], item["hits"])
        else:
            ok = send_telegram(cfg, item["message"])
        if ok:
            db.execute("DELETE FROM outbox WHERE post_key = ? AND sink = ?", (key, sink))
        else:
            db.execute("UPDATE outbox SET attempts = attempts + 1 WHERE post_key = ? AND sink = ?", (key, sink))
        db.commit()
    pending, failing = db.execute("SELECT COUNT(*), COALESCE(SUM(attempts >= 3), 0) FROM outbox").fetchone()
    return pending, failing


def cache_posts(db, posts, group_id):
    """Remember every scanned post's link, so later scans don't revisit it."""
    with db:
        for post in posts:
            db.execute("INSERT OR IGNORE INTO post_cache VALUES (?, ?, ?, ?)",
                       (group_id, fbn_patch.fingerprint(post["author"], post["text"]), post["url"], int(time.time())))


def record_posts(db, posts, group_id, group_name, bootstrap, match):
    """Mark new posts seen and queue deliveries for matches, in one transaction per post."""
    new = 0
    cache_posts(db, posts, group_id)
    for post in posts:
        if is_seen(db, post["key"]):
            continue
        new += 1
        with db:
            mark_seen(db, post["key"], group_id)
            hits = [] if bootstrap else match(post["text"])
            if hits:
                message = (f"🔔 [{group_name}] {post['author']}\n"
                           f"Keywords: {', '.join(hits)}\n\n"
                           f"{post['text'][:1500]}\n\n"
                           f"{post['url']}")
                enqueue(db, post["key"], "telegram", {"message": message})
                enqueue(db, post["key"], "braveduck",
                        {"post": post, "group_id": group_id, "group_name": group_name, "hits": hits})
    return new


def cmd_run():
    cfg = load_config()
    match = build_matcher(cfg["keywords"])
    db = open_db()
    lo, hi = cfg["poll_seconds"]
    policy = ScanPolicy(sample_count=cfg["sample_count"], max_scrolls=cfg["scrolls"])
    headless = cfg.get("headless", True)
    accounts = load_accounts(cfg)

    failing = {}  # (account, group id) -> (group name, first failure time, consecutive failed rounds, error type)
    gaps = {}  # (account, group id) -> (group name, consecutive incomplete rounds)
    down = {}  # account name -> reason; its groups are skipped until you log it in again and restart
    while True:
        # Board text uses error types and our own identifiers only, never fbn's messages or post text.
        scanned, last_error = 0, ""
        for account in accounts:
            if account["name"] in down:
                continue
            for group_ref, name in account["groups"]:
                gid = group_ref.key
                key = (account["name"], gid)
                # First successful scan of a group records existing posts silently instead of flooding alerts.
                bootstrap = not group_initialized(db, gid)
                try:
                    posts, complete = run_scan(account, group_ref, policy, get_boundary(db, gid), headless,
                                               known_links(db, gid))
                except FATAL_ERRORS as e:
                    down[account["name"]] = f"{type(e).__name__} on account {account['name']}"
                    send_telegram(cfg, f"⚠️ fb-watcher: account {account['name']} stopped ({type(e).__name__}): {e}\n"
                                       f"Its groups are paused. Run `python watcher.py login {account['name']}` "
                                       "(or import-cookies), then restart the service.")
                    print(f"[{account['name']}] {type(e).__name__}: {e} (account paused)")
                    break
                except FbnError as e:
                    # Timeouts, layout drift, access problems, a hung scan: skip this group for one round.
                    print(f"[{name}] {type(e).__name__}: {e} (will retry next round)")
                    last_error = f"{type(e).__name__} on {name} ({gid})"
                    _, since, rounds, _ = failing.get(key, (name, time.strftime("%H:%M"), 0, ""))
                    failing[key] = (name, since, rounds + 1, type(e).__name__)
                    continue
                scanned += 1
                failing.pop(key, None)
                new = record_posts(db, posts, gid, name, bootstrap, match)
                if bootstrap:
                    mark_group_initialized(db, gid)
                incomplete = advance_boundary(db, gid, posts, complete)
                if incomplete:
                    gaps[key] = (name, incomplete)
                else:
                    gaps.pop(key, None)
                print(f"[{time.strftime('%H:%M:%S')}] [{account['name']}/{name}] {len(posts)} posts, {new} new"
                      + (" (bootstrap, no alerts)" if bootstrap else "")
                      + (f" (didn't reach last scan's newest post, {incomplete} rounds)" if incomplete else ""))
                time.sleep(random.randint(3, 8))

        pending, failing_deliveries = deliver_pending(cfg, db)
        problems = list(down.values())
        problems += [f"{err} on {name} ({k[1]}) since {since}"
                     for k, (name, since, rounds, err) in failing.items() if rounds >= 3]
        problems += [f"incomplete scans on {name} ({k[1]}) for {rounds} rounds"
                     for k, (name, rounds) in gaps.items() if rounds >= INCOMPLETE_ROUNDS_BEFORE_GAP]
        if failing_deliveries:
            problems.append(f"{failing_deliveries} of {pending} deliveries failing")
        if len(down) == len(accounts):
            report_to_braveduck(cfg, False, "; ".join(problems))
            raise SystemExit(EXIT_NEEDS_USER)
        if problems:
            report_to_braveduck(cfg, False, "; ".join(problems))
        else:
            report_to_braveduck(cfg, scanned > 0, last_error)
        time.sleep(random.randint(lo, hi))


def braveduck_api(cfg):
    """The braveduck scanner API from config `braveduck_service` {url, key_file}."""
    import braveduck_service as bds

    svc = dict(cfg.get("braveduck_service") or {})
    svc.setdefault("url", os.environ.get("BRAVEDUCK_SCANNER_URL", ""))
    svc.setdefault("key_file", os.environ.get("BRAVEDUCK_SCANNER_KEY_FILE", ""))
    if not svc.get("url") or not svc.get("key_file"):
        raise SystemExit("set braveduck_service {\"url\", \"key_file\"} in config.json, "
                         "or BRAVEDUCK_SCANNER_URL and BRAVEDUCK_SCANNER_KEY_FILE")
    key = Path(os.path.expanduser(svc["key_file"])).read_text(encoding="utf-8").strip()
    return bds.ScannerAPI(svc["url"], key)


def cmd_check_group(link, account_name=None):
    import braveduck_service as bds

    account = find_account(load_config(), account_name)
    ref = parse_group_ref(link)
    name, vis, note, numeric = bds.check_group(account["profile"], account["proxy"],
                                               f"https://m.facebook.com/groups/{ref.key}/", ref.key)
    print(json.dumps({"key": ref.key, "name": name, "visibility": vis, "note": note, "numeric_id": numeric},
                     ensure_ascii=False, indent=2))


def cmd_serve_braveduck():
    """Scan the groups braveduck's users follow: check each group's page for its
    name and visibility, scan public ones, hand new posts to braveduck."""
    import braveduck_service as bds

    cfg = load_config()
    api = braveduck_api(cfg)
    account = load_accounts(cfg)[0]  # one account reads every followed group
    db = open_db()
    db.execute("CREATE TABLE IF NOT EXISTS group_alias (key TEXT PRIMARY KEY, numeric_id TEXT NOT NULL)")
    lo, hi = cfg["poll_seconds"]
    policy = ScanPolicy(sample_count=cfg["sample_count"], max_scrolls=cfg["scrolls"])
    headless = cfg.get("headless", True)
    rng = random.Random()
    while True:
        try:
            groups = api.groups()
        except (OSError, ValueError) as e:
            print(f"braveduck unreachable: {type(e).__name__} (retrying)")
            bds.sleep_between(lo, hi, rng)
            continue
        for g in groups:
            key = g["key"]
            try:
                if bds.due_for_check(g):
                    name, vis, note, numeric = bds.check_group(account["profile"], account["proxy"],
                                                               f"https://m.facebook.com/groups/{key}/", key, headless)
                    if numeric:
                        db.execute("INSERT OR REPLACE INTO group_alias VALUES (?, ?)", (key, numeric))
                        db.commit()
                    api.status(key, vis, name, note)
                    g["visibility"] = vis
                    print(f"[{key}] {vis} {name!r} {note}")
                    time.sleep(rng.randint(3, 8))
                if g["visibility"] != "public":
                    continue
                row = db.execute("SELECT numeric_id FROM group_alias WHERE key = ?", (key,)).fetchone()
                numeric = key if key.isdecimal() else (row[0] if row else None)
                if not numeric:
                    continue  # checked next round
                gid = numeric
                posts, complete = run_scan(account, parse_group_ref(gid), policy, get_boundary(db, gid), headless,
                                           known_links(db, gid))
                cache_posts(db, posts, gid)
                fresh = bds.new_posts(posts, lambda k: is_seen(db, k))
                if fresh:
                    res = api.posts(key, fresh)
                    with db:
                        for p in fresh:
                            mark_seen(db, p["key"], gid)
                    print(f"[{key}] {len(fresh)} new post(s) → {res}")
                advance_boundary(db, gid, posts, complete)
                time.sleep(rng.randint(3, 8))
            except FATAL_ERRORS as e:
                send_telegram(cfg, f"⚠️ fb-watcher (braveduck scanner): account stopped ({type(e).__name__}). Log in again, then restart.")
                raise SystemExit(EXIT_NEEDS_USER)
            except (FbnError, OSError, ValueError) as e:
                print(f"[{key}] {type(e).__name__}: {e} (next round)")
        bds.sleep_between(lo, hi, rng)


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
    elif cmd == "scan-worker" and not rest:
        cmd_scan_worker()
    elif cmd == "serve-braveduck" and not rest:
        cmd_serve_braveduck()
    elif cmd == "check-group" and len(rest) in (1, 2):
        cmd_check_group(*rest)
    elif cmd == "fetch-post":
        import fetch_post
        sys.exit(fetch_post.main(rest))
    elif cmd == "test-alert":
        send_telegram(load_config(), "✅ fb-watcher test alert")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
