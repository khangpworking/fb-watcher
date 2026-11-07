"""fb-watcher as braveduck's Facebook scanner (the shared server's groups).

braveduck users follow Facebook groups by pasting a group's link. braveduck
never opens Facebook; this scanner does, with its own logged-in account:

  1. GET  /groups               the groups any user follows
  2. check each new (or stale) group's page: its name and whether it's a
     public group, then POST /groups/<key>/status
  3. scan public groups only, and POST /groups/<key>/posts with new posts

braveduck matches posts to each user's own search terms. A group counts as
public only when its page says so in so many words; anything unclear is
reported as "unavailable" and nothing from it is shared, because a private
group's posts are only for its members.
"""

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


class ScannerAPI:
    """braveduck's scanner API (`serve --scanner-addr`), with its key."""

    def __init__(self, url, key, timeout=30):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"null")

    def groups(self):
        return self._call("GET", "/groups") or []

    def status(self, key, visibility, name="", note=""):
        return self._call("POST", f"/groups/{urllib.request.quote(key, safe='')}/status",
                          {"name": name, "visibility": visibility, "note": note})

    def posts(self, key, posts):
        return self._call("POST", f"/groups/{urllib.request.quote(key, safe='')}/posts",
                          {"posts": [{"url": p["url"], "text": p["text"]} for p in posts]})


# The group page states its privacy under the group's name, e.g.
# "Public group · 12K members" / "Nhóm Công khai · 12K thành viên".
# A group page's own header reads "Public group · 2.9K members" (on today's page the
# parts sit on separate lines with a non-breaking space). The bare words "Public group"
# also appear in group names and in suggested groups, so only this form counts.
_HEADER_LABEL = re.compile(
    r"(?<![\w])(public group|private group|nhóm công khai|nhóm riêng tư)\s*·\s*[\d.,]+\s*[kmn]?\S*\s*"
    r"(members|thành viên)", re.IGNORECASE)
# Only the top of the page: further down, suggested groups carry labels too.
HEADER_CHARS = 1500
_UNAVAILABLE = ("this content isn't available", "nội dung này hiện không hiển thị", "this page isn't available",
                "trang này hiện không hiển thị")
_OWN_ID = [r"fb://group/(?:\?id=)?{id}\b", r'"groupID":"{id}"', r'"group_id":"{id}"']


def parse_visibility(page_text, after=0):
    """'public' or 'private' from the first header label ("Public group · N members") in
    the top of a group page's text, starting at `after`; None if there's none."""
    head = (page_text or "")[:HEADER_CHARS]
    match = _HEADER_LABEL.search(head, after)
    if not match:
        return None
    return "public" if match.group(1).casefold() in ("public group", "nhóm công khai") else "private"


def parse_name(title):
    """The group's name from the page title ("Name | Facebook")."""
    name = (title or "").strip()
    for suffix in (" | Facebook", "| Facebook"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name[:200]


# Where a group page names its numeric id (fbn reads groups by number).
_GROUP_ID = [re.compile(r"fb://group/(?:\?id=)?(\d{5,20})"),
             re.compile(r'"groupID":"(\d{5,20})"'),
             re.compile(r"facebook\.com/groups/(\d{5,20})/")]


def parse_group_id(html, key):
    """The group's numeric id: the key itself when it's a number, else the
    first id the page names (None if it names none)."""
    if key.isdecimal():
        return key
    for pattern in _GROUP_ID:
        m = pattern.search(html or "")
        if m:
            return m.group(1)
    return None


def classify_group_page(final_url, title, header_text, html, key):
    """Strict visibility for a loaded group page: (visibility, note, numeric_id).

    Public only when the page is this group's own page (its URL, and an ID the page itself
    names) and its header reads "Public group · N members" close after the group's name.
    Login/checkpoint pages raise AuthenticationRequiredError; anything unclear is
    'unavailable', so a mistake can only hide a group, never expose its posts."""
    from urllib.parse import urlsplit

    from fbn.exceptions import AuthenticationRequiredError

    path = urlsplit(final_url).path
    if "/login" in path or "/checkpoint" in path:
        raise AuthenticationRequiredError("Facebook asked the account to log in again.")
    text = (header_text or "")[:HEADER_CHARS]
    if any(marker in text.casefold() for marker in _UNAVAILABLE):
        return "unavailable", "the group's page isn't available to this account", None
    if not path.startswith("/groups/"):
        return "unavailable", "didn't land on the group's page", None
    numeric = key if key.isdecimal() else parse_group_id(html, key)
    if not numeric or not any(re.search(p.format(id=re.escape(numeric)), html or "") for p in _OWN_ID):
        return "unavailable", "couldn't confirm this is the group's own page", None
    name = parse_name(title).casefold()
    start = text.casefold().find(name) if name else -1
    vis = parse_visibility(text, max(start, 0))
    match = _HEADER_LABEL.search(text, max(start, 0))
    if vis is None or (start >= 0 and match.start() - start > 400):
        return "unavailable", "couldn't read the group's privacy label", numeric
    return vis, "", numeric


def load_group_page(profile_dir, proxy, key, headless=True, timeout_ms=60_000):
    """Open a group's page with the account; return (final_url, title, header_text, html).
    Navigation off facebook.com is aborted; a login form raises AuthenticationRequiredError."""
    from fbn.exceptions import AuthenticationRequiredError
    from fbn.mobile import _trusted_url
    from playwright.sync_api import sync_playwright

    options = {"headless": headless}
    if proxy:
        options["proxy"] = proxy
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), **options)
        try:
            ctx.route("**/*", lambda route: route.abort()
                      if route.request.is_navigation_request() and not _trusted_url(route.request.url)
                      else route.fallback())
            page = ctx.new_page()
            page.goto(f"https://m.facebook.com/groups/{key}/", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)  # the header renders after load
            if page.locator('input[name="pass"]').count():
                raise AuthenticationRequiredError("Facebook asked the account to log in again.")
            main = page.locator('[role="main"]')
            surface = main.first if main.count() else page.locator("body").first
            return page.url, page.title(), surface.inner_text(timeout=10_000)[:HEADER_CHARS], page.content()
        finally:
            ctx.close()


def check_group(profile_dir, proxy, url, key, headless=True, timeout_ms=60_000):
    """Open a group's page with the scanner's account and return
    (name, visibility, note, numeric_id); visibility is 'public', 'private'
    or 'unavailable' (see classify_group_page). `url` is kept for callers; the
    group's page is always m.facebook.com/groups/<key>/."""
    final_url, title, text, html = load_group_page(profile_dir, proxy, key, headless, timeout_ms)
    vis, note, numeric = classify_group_page(final_url, title, text, html, key)
    return parse_name(title), vis, note, numeric


def due_for_check(group, now=None, recheck=timedelta(hours=24)):
    """Whether a group's visibility needs (re)checking: new, never checked,
    or checked longer ago than recheck."""
    if group.get("visibility") in (None, "", "checking"):
        return True
    checked = group.get("checked_at") or ""
    try:
        at = datetime.fromisoformat(checked.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) - at >= recheck


def new_posts(posts, is_seen):
    """The posts not handed over before, oldest first."""
    return [p for p in reversed(posts) if p.get("url") and p.get("text") and not is_seen(p["key"])]


def sleep_between(lo, hi, rng):
    time.sleep(rng.randint(lo, hi))
