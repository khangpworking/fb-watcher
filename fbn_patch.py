"""fb-watcher's patches to fbn (pinned 982cc97 in requirements.txt).

Each patch replaces an exact piece of fbn's page script or one function. A script
patch whose text is no longer found raises at import, so an fbn update can't
silently drop a fix: re-check this file whenever you bump the fbn commit.

1. Skip unreadable cards instead of failing the page.
2. Recognise posts with an "AI content" label and posts the account already liked.
3. Expose the parser's own timestamp node for each post.
4. Get post links by tapping the timestamp (Facebook's "..." menu broke on 2026-11-04).
5. One post whose link can't be read ends the scan early with the posts read so far,
   instead of failing the group.
6. Keep a post whose own page fbn can't render, but only if its identity still matches.
"""

import logging
import time
from urllib.parse import parse_qs, urlsplit

import fbn.mobile as mobile
from playwright.sync_api import Error as PlaywrightError

log = logging.getLogger("fbn_patch")

_SCRIPT_PATCHES = [
    # 1. fbn's feed script already skips a card it can't parse (a shared post, a
    # "Follow" button by the author, ...) but also adds its problem to the page-wide
    # `missing` list, which fails the whole page. Keep only the page-level problem.
    (
        "hasFeed: roots.length === 1, missing: Array.from(missing),",
        "hasFeed: roots.length === 1,"
        " missing: Array.from(missing).filter(name => name === 'unique_mobile_scroller'),"
        " skippedCards: Array.from(missing),",
        1,
    ),
    # 2a. "AI content, 8 minutes ago" is a badge rendered as a link, so fbn didn't
    # strip it as a timestamp prefix and dropped the post.
    (
        "visible(child) && !child.closest('[role=\"link\"],a')\n"
        "            && !child.querySelector('[role=\"link\"],a')",
        "visible(child) && ((!child.closest('[role=\"link\"],a')\n"
        "            && !child.querySelector('[role=\"link\"],a'))"
        " || clean(child.innerText) === 'AI content')",
        1,
    ),
    # 2b. An already-liked post's button reads "1LIKEpressed, ..." -> "likepressed".
    ("(?:like|unlike)(?:,|$)", "(?:like|unlike)(?:pressed)?(?:,|$)", 2),
    # 3. The timestamp node the parser proved belongs to this post (after any
    # author/badge prefix), so the link step never re-guesses it.
    (
        "records.push({card, menu, expand, regions: body.regions,",
        "records.push({card, menu, expand, timestampNode: timestamp.node, regions: body.regions,",
        1,
    ),
]

for name in ("MOBILE_SCAN_SCRIPT", "MOBILE_PERMALINK_SCAN_SCRIPT"):
    script = getattr(mobile, name)
    for old, new, count in _SCRIPT_PATCHES:
        if script.count(old) != count:
            raise RuntimeError(f"fbn_patch: {name} changed upstream ({old[:40]!r}); update fbn_patch.py")
        script = script.replace(old, new)
    setattr(mobile, name, script)


# 4. Tapping a post's "..." menu on m.facebook.com now jumps to "This Page is not
# available", so "Copy link" is gone. Tapping the post's timestamp still opens
# /story.php?story_fbid=<post>&id=<group>; read the IDs there and go back to the feed.
# fbn verifies the link afterwards. Found and tested by Astra (codex): astra-debug/.
_TIMESTAMP = r'''menu => {
  const record = (SCRIPT)(true).records.find(r => r.menu === menu);
  if (!record || !record.timestampNode || !record.timestampNode.isConnected) {
    throw new Error('source timestamp missing');
  }
  return record.timestampNode;
}'''.replace("SCRIPT", mobile.MOBILE_SCAN_SCRIPT)


def _timestamp_post_link(page, menu, *, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    source_url = page.url
    group = mobile._group_key(source_url)
    if not group or not group.isdecimal():
        raise mobile._layout_failure("numeric_source_group")
    try:
        stamp = page.evaluate_handle(_TIMESTAMP, menu).as_element()
    except PlaywrightError:
        # fbn's parser only counts visible cards; after scrolling, this one may sit
        # at the viewport edge. Bring it into view and look once more.
        menu.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(300)
        stamp = page.evaluate_handle(_TIMESTAMP, menu).as_element()
    if stamp is None:
        raise mobile._layout_failure("source_timestamp")
    try:
        # Dispatch to the timestamp: its containing header owns the action.
        stamp.dispatch_event("click")
        page.wait_for_url(
            lambda url: url != source_url,
            timeout=mobile._remaining_seconds(deadline, "timestamp_post_link") * 1000,
        )
        destination = urlsplit(page.url)
        query = parse_qs(destination.query)
        ids, groups = query.get("story_fbid", []), query.get("id", [])
        if (destination.scheme != "https" or destination.hostname != "m.facebook.com"
                or destination.path != "/story.php" or groups != [group]
                or len(ids) != 1 or not ids[0].isdecimal()):
            raise mobile._layout_failure("timestamp_post_in_source_group")
        return f"https://www.facebook.com/groups/{group}/posts/{ids[0]}/"
    finally:
        try:
            # Cleanup gets its own budget, even if the step above ran out of time.
            if page.url != source_url:
                page.go_back(wait_until="domcontentloaded", timeout=5000)
                page.wait_for_url(source_url, timeout=5000)
        finally:
            stamp.dispose()


# 5. fbn's scan loop has no way to skip one card, so a single post whose link step
# fails used to throw away the whole group's round. If the feed is back where it was,
# end the scan early instead: fbn returns the posts read so far as a bounded
# (incomplete) result, and watcher.py keeps the old boundary for incomplete scans.
partial_scans = []  # (reason class) of scans ended early, read by watcher.py for its log


def copy_post_link(page, menu, *, timeout_seconds):
    source_url = page.url
    try:
        return _timestamp_post_link(page, menu, timeout_seconds=timeout_seconds)
    except mobile._ScanBudgetExpired:
        raise
    except (PlaywrightError, mobile.LayoutChangedError) as exc:
        try:
            restored = page.url == source_url and menu.evaluate("n => n.isConnected")
        except PlaywrightError:
            restored = False
        if not restored:
            raise
        partial_scans.append(type(exc).__name__)
        # Local log only (journal); board health never gets this text.
        detail = (str(exc).strip().splitlines() or [""])[0][:200]
        log.warning("post link step failed (%s: %s); ending this scan with the posts read so far",
                    type(exc).__name__, detail)
        raise mobile._ScanBudgetExpired() from exc


mobile.copy_post_link = copy_post_link


# 6. fbn also opens each post's own page to verify the link. Some posts' pages can't
# be rendered by fbn (e.g. the "Copilot" guide post), which failed the group on every
# round. Accept the link only when the failure was that render step and the page's own
# URL, if it names a post, names the same one. A proven mismatch still fails.
_resolve_post_link = mobile.resolve_post_link
unverified = []  # post IDs accepted without the full page check, for diagnostics


def resolve_post_link(target, copied, payload, allowed_group_keys, **kwargs):
    try:
        return _resolve_post_link(target, copied, payload, allowed_group_keys, **kwargs)
    except mobile.LayoutChangedError as exc:
        if getattr(exc, "_fbn_failure_phase", None) != "page_render":
            raise
        link = mobile.parse_post_url(copied)
        final = mobile.parse_post_url(target.url)
        if link is None or link.group_key not in allowed_group_keys:
            raise
        if final is not None and final.post_id != link.post_id:
            raise
        unverified.append(link.post_id)
        del unverified[:-50]
        return link.url


mobile.resolve_post_link = resolve_post_link
