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
_PUBLIC = re.compile(r"(?<![\w])(public group|nhóm công khai)(?![\w])")
_PRIVATE = re.compile(r"(?<![\w])(private group|nhóm riêng tư)(?![\w])")
# Only the top of the page: further down, suggested groups carry labels too.
HEADER_CHARS = 1500


def parse_visibility(page_text):
    """'public' or 'private' from a group page's text, or None when the page
    doesn't say exactly one of them near the top."""
    head = (page_text or "")[:HEADER_CHARS].casefold()
    public, private = bool(_PUBLIC.search(head)), bool(_PRIVATE.search(head))
    if public == private:
        return None
    return "public" if public else "private"


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


def check_group(profile_dir, proxy, url, key, headless=True, timeout_ms=60_000):
    """Open a group's page with the scanner's account and return
    (name, visibility, note, numeric_id); visibility is 'public', 'private'
    or 'unavailable'. Reads only the page's title, its top text and the
    group's id."""
    from playwright.sync_api import sync_playwright

    options = {"headless": headless}
    if proxy:
        options["proxy"] = proxy
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), **options)
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)  # the header renders after load
            main = page.locator('[role="main"]')
            surface = main.first if main.count() else page.locator("body").first
            text = surface.inner_text(timeout=10_000)
            name = parse_name(page.title())
            numeric = parse_group_id(page.content(), key)
        finally:
            ctx.close()
    vis = parse_visibility(text)
    if vis is None:
        return name, "unavailable", "couldn't tell whether this group is public", numeric
    if vis == "public" and numeric is None:
        return name, "unavailable", "couldn't find this group's number", None
    return name, vis, "", numeric


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
