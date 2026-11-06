"""Fetch one Facebook group post by URL, on demand.

    from fetch_post import fetch_post, FetchError
    post = fetch_post("https://www.facebook.com/groups/<group>/posts/<post>/", account="default")

Returns a dict with the versioned schema below (SCHEMA). Uses the account's own profile and
proxy, but the desktop site: since 2026-11-05 Facebook answers direct loads of mobile post
pages with an empty body, while the desktop post page renders in full. It reads only the
one post (never a feed). See README "Fetch one post".
"""

import json
import mimetypes
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import watcher  # sets up Chromium's libs before Playwright starts it
import dateparser
from fbn.exceptions import ProfileInUseError
from fbn.extractor import parse_post_url
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

SCHEMA = 1

# Exit codes for `watcher.py fetch-post`; FetchError.code carries the same value.
EXIT_OK = 0
EXIT_ERROR = 1        # anything else (layout change, network, timeout)
EXIT_USAGE = 2        # bad arguments or not a supported Facebook group post URL
EXIT_LOGIN = 3        # account not logged in, or Facebook wants an account check
EXIT_NOT_VISIBLE = 4  # post deleted, not visible to this account, or group not joined
EXIT_BUSY = 5         # the account's browser profile stayed busy (the watcher is scanning)

LOCK_WAIT_SECONDS = 240
PAGE_TIMEOUT_SECONDS = 45

# Reads the post out of the desktop post page (a dialog over the group). Returns null
# until the post's message has rendered.
_POST_JS = r"""gid => {
  const MSG = '[data-ad-rendering-role="story_message"],[data-ad-preview="message"],[data-ad-comet-preview="message"]';
  const roots = [...document.querySelectorAll('[role="dialog"]')].filter(d => d.querySelector(MSG));
  const root = roots[0] || document.querySelector('[role="main"]');
  const msg = root && root.querySelector(MSG);
  if (!msg) return null;
  const before = node => node.compareDocumentPosition(msg) & Node.DOCUMENT_POSITION_FOLLOWING;
  const links = [...root.querySelectorAll('a[href]')];
  const path = a => { try { return new URL(a.href).pathname; } catch (e) { return ''; } };
  const group = links.find(a => path(a) === '/groups/' + gid + '/' && a.innerText.trim());
  const author = links.filter(before).find(a => a.innerText.trim()
    && /^\/(groups\/[^/]+\/user\/\d+|profile\.php|people\/)/.test(path(a)));
  // Comments are their own articles; keep images of the post only.
  const inComment = img => { const art = img.closest('[role="article"]'); return art && !art.contains(msg); };
  const images = [...new Set([...root.querySelectorAll('img')]
    .filter(img => img.naturalWidth >= 200 && img.naturalHeight >= 120 && !inComment(img)
      && /^https:\/\/[^/]*fbcdn\.net\//.test(img.currentSrc || img.src))
    .map(img => img.currentSrc || img.src))];
  // The date link's text is scrambled (U+034F); its tooltip shows the real date.
  const dateLink = links.filter(before).find(a => a.innerText.includes('\u034f'));
  document.querySelectorAll('[data-fbw-date]').forEach(a => a.removeAttribute('data-fbw-date'));
  if (dateLink) dateLink.setAttribute('data-fbw-date', '1');
  const postLink = links.map(a => a.href).find(h => /\/groups\/[^/]+\/(posts|permalink)\/\d+/.test(h));
  return {
    text: msg.innerText.trim(), author: author ? author.innerText.trim() : null,
    group_name: group ? group.innerText.trim() : null, images,
    has_date_link: !!dateLink,
    post_link: postLink || null,
    see_more: [...msg.querySelectorAll('[role="button"]')].some(b => /^(See more|Xem thêm)$/.test(b.innerText.trim())),
  };
}"""

_NOT_VISIBLE_TEXT = ("This content isn't available", "Nội dung này hiện không hiển thị",
                     "content isn't available right now", "Join group to")


class FetchError(Exception):
    def __init__(self, code, kind, message):
        super().__init__(message)
        self.code, self.kind = code, kind


def _supported_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"):
        return False
    return parse_post_url(_desktop_url(url)) is not None or (re.fullmatch(r"/share/p/[A-Za-z0-9]+/?", parts.path) is not None
                                                     and not parts.query and not parts.fragment)


def _desktop_url(url):
    parts = urlsplit(url)
    return parts._replace(netloc="www.facebook.com").geturl()


def _account(account):
    if isinstance(account, dict):
        return account
    return watcher.find_account(watcher.load_config(), account)


def _download(urls, assets_dir, post_id):
    assets_dir = Path(assets_dir).expanduser()
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, url in enumerate(urls, 1):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            ext = mimetypes.guess_extension(response.headers.get_content_type()) or ".jpg"
            path = assets_dir / f"{post_id}-{i}{'.jpg' if ext == '.jpe' else ext}"
            path.write_bytes(response.read())
        paths.append(str(path))
    return paths


def _post_date(page, tz_name):
    if not page.locator("[data-fbw-date]").count():
        return None, None
    link = page.locator("[data-fbw-date]").first
    try:
        link.hover(timeout=5000)
        page.locator('[role="tooltip"]').first.wait_for(timeout=4000)
        label = page.locator('[role="tooltip"]').first.inner_text().strip()
    except Exception:
        return None, None
    when = dateparser.parse(label, settings={"TIMEZONE": tz_name, "RETURN_AS_TIMEZONE_AWARE": True})
    return label, when


def _read_post(context, url, tz_name):
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_SECONDS * 1000)
    found, deadline = None, time.monotonic() + PAGE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if "/login" in page.url or "/checkpoint" in page.url or page.locator('input[name="pass"]').count():
            raise FetchError(EXIT_LOGIN, "login_required", "the account isn't logged in or needs an account check")
        link = parse_post_url(page.url)
        found = page.evaluate(_POST_JS, link.group_key if link else "")
        if found:
            break
        body = page.evaluate("document.body ? document.body.innerText : ''")
        if any(marker in body for marker in _NOT_VISIBLE_TEXT):
            raise FetchError(EXIT_NOT_VISIBLE, "not_visible",
                             "post not visible to this account (deleted, private, or group not joined)")
        page.wait_for_timeout(500)
    if not found:
        raise FetchError(EXIT_NOT_VISIBLE, "not_visible", "no post rendered on that page")
    if found["see_more"]:
        page.locator('[role="dialog"] [role="button"]', has_text=re.compile(r"^(See more|Xem thêm)$")).first.click()
        page.wait_for_timeout(800)
        found = page.evaluate(_POST_JS, parse_post_url(page.url).group_key if parse_post_url(page.url) else "")
    link = parse_post_url(page.url) or parse_post_url(found["post_link"] or "")
    if link is None:
        raise FetchError(EXIT_NOT_VISIBLE, "not_visible", "the link didn't lead to a group post")
    if not found["group_name"]:  # the group key wasn't known before a share link resolved
        found = page.evaluate(_POST_JS, link.group_key) or found
    label, when = _post_date(page, tz_name)
    observed = datetime.now(timezone.utc)
    return {
        "schema": SCHEMA,
        "url": f"https://www.facebook.com/groups/{link.group_key}/posts/{link.post_id}/",
        "post_id": link.post_id,
        "group_id": link.group_key if link.group_key.isdecimal() else None,
        "group_key": link.group_key,
        "group_name": found["group_name"],
        "author": found["author"],
        "text": found["text"],
        "published_at": when.isoformat() if when else None,
        "published_label": label,
        "images": found["images"],
        "image_paths": [],
        "fetched_at": observed.isoformat(),
    }


def fetch_post(url, account=None, *, assets_dir=None, lock_wait=LOCK_WAIT_SECONDS, headless=True):
    """Fetch one group post. `account` is an account name from config (default: the first
    account) or an account dict from watcher.load_accounts. Raises FetchError."""
    if not isinstance(url, str) or not _supported_url(url.strip()):
        raise FetchError(EXIT_USAGE, "bad_url", "not a Facebook group post or share link")
    url = _desktop_url(url.strip())
    try:
        acct = _account(account)
    except SystemExit as e:  # find_account exits on an unknown name
        raise FetchError(EXIT_USAGE, "bad_account", str(e)) from None
    tz_name = watcher.load_config().get("timezone", "Asia/Ho_Chi_Minh") if not isinstance(account, dict) \
        else "Asia/Ho_Chi_Minh"
    source = watcher.make_source(acct, headless)  # only for fbn's profile lock
    profile_dir = source._prepare_profile()
    give_up = time.monotonic() + lock_wait
    while True:
        try:
            # Holds the profile lock for the whole read; the watcher's scans wait for it.
            with source._profile_lock(profile_dir), sync_playwright() as pw:
                options = {"proxy": acct["proxy"]} if acct.get("proxy") else {}
                context = pw.chromium.launch_persistent_context(
                    str(profile_dir), headless=headless, viewport={"width": 1280, "height": 900},
                    timezone_id=tz_name, accept_downloads=False, **options)
                try:
                    post = _read_post(context, url, tz_name)
                finally:
                    context.close()
            break
        except ProfileInUseError:
            if time.monotonic() >= give_up:
                raise FetchError(EXIT_BUSY, "profile_busy",
                                 f"account profile still busy after {lock_wait}s") from None
            time.sleep(3)
        except PlaywrightError as e:
            raise FetchError(EXIT_ERROR, "error", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}") from e
    if assets_dir and post["images"]:
        post["image_paths"] = _download(post["images"], assets_dir, post["post_id"])
    return post


def main(args):
    """CLI: fetch-post URL [ACCOUNT] [--json] [--assets-dir DIR]. Returns the exit code."""
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    assets_dir = None
    if "--assets-dir" in args:
        i = args.index("--assets-dir")
        if i + 1 >= len(args):
            print("--assets-dir needs a directory")
            return EXIT_USAGE
        assets_dir = args[i + 1]
        del args[i:i + 2]
    if len(args) not in (1, 2):
        print("usage: watcher.py fetch-post URL [ACCOUNT] [--json] [--assets-dir DIR]")
        return EXIT_USAGE
    try:
        post = fetch_post(args[0], args[1] if len(args) == 2 else None, assets_dir=assets_dir)
    except FetchError as e:
        if as_json:
            print(json.dumps({"schema": SCHEMA, "error": e.kind, "message": str(e)}, ensure_ascii=False))
        else:
            print(f"fetch-post failed ({e.kind}): {e}")
        return e.code
    except Exception as e:  # unexpected: still one JSON object for callers
        if as_json:
            print(json.dumps({"schema": SCHEMA, "error": "error", "message": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False))
        else:
            print(f"fetch-post failed: {type(e).__name__}: {e}")
        return EXIT_ERROR
    if as_json:
        print(json.dumps(post, ensure_ascii=False, indent=2))
    else:
        print(f"{post['author']} in {post['group_name']} ({post['published_label'] or '?'})\n"
              f"{post['url']}\n\n{post['text']}")
        for path in post["image_paths"] or post["images"]:
            print("image:", path)
    return EXIT_OK
