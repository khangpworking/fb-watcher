"""Make fbn skip posts it can't fully read instead of failing the whole scan.

fbn's feed script already skips a card it can't parse (a shared post, a "Follow"
button beside the author, ...), but it also adds that card's problem to the
page-wide `missing` list, and any entry there fails the page with
LayoutChangedError. So one odd post hid every other post in the group.

This keeps only the page-level problem (no single feed scroller) in `missing`;
per-card problems move to `skippedCards`. Written against fbn @ 982cc97:
re-check it when you bump the fbn commit in requirements.txt.
"""

import fbn.mobile as mobile

_ORIGINAL = "hasFeed: roots.length === 1, missing: Array.from(missing),"
_PATCHED = (
    "hasFeed: roots.length === 1,"
    " missing: Array.from(missing).filter(name => name === 'unique_mobile_scroller'),"
    " skippedCards: Array.from(missing),"
)

for name in ("MOBILE_SCAN_SCRIPT", "MOBILE_PERMALINK_SCAN_SCRIPT"):
    script = getattr(mobile, name)
    if _ORIGINAL not in script:
        raise RuntimeError(f"fbn_patch: {name} changed upstream; update fbn_patch.py for the new fbn version")
    setattr(mobile, name, script.replace(_ORIGINAL, _PATCHED))


# fbn also verifies each post's copied link by opening the post's own page. If fbn
# can't read that page, the whole group scan fails, on every round, as long as the
# post stays near the top. In that case, accept the post's own link if Facebook's
# "Copy link" gave one directly, or if the share link (/share/p/...) redirected to
# one in this group; otherwise fail as before.
_resolve_post_link = mobile.resolve_post_link


def resolve_post_link(target, copied, payload, allowed_group_keys, **kwargs):
    try:
        return _resolve_post_link(target, copied, payload, allowed_group_keys, **kwargs)
    except mobile.LayoutChangedError:
        link = mobile.parse_post_url(copied) or mobile.parse_post_url(target.url)
        if link is None or link.group_key not in allowed_group_keys:
            raise
        unverified.append(link.post_id)
        return link.url


unverified = []  # post IDs accepted on the copied link alone, for diagnostics
mobile.resolve_post_link = resolve_post_link


# Facebook's mobile post "..." menu broke on 2026-11-04: tapping it jumps to "This Page
# is not available", so fbn can't use "Copy link". Instead, tap the post's own timestamp
# (it opens /story.php?story_fbid=<post>&id=<group>), read the post ID, and go back to the
# feed. fbn still verifies the link afterwards. Found and tested by Astra (codex), see
# astra-debug/report.md.
import time  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402


_TIMESTAMP = r'''menu => {
  const script = SCRIPT;
  const record = script(true).records.find(r => r.menu === menu);
  if (!record) throw new Error('source card missing');
  const nodes = [...menu.parentElement.querySelectorAll('[aria-label]')]
    .filter(n => n !== menu && n.getAttribute('aria-label').normalize('NFKC').replace(/[\u034f\u200b\u200e\u200f\ufeff]/g, '').replace(/\s+/g, ' ').trim().replace(/^[,\s]+/, '') === record.payload.timestampLabel);
  if (nodes.length !== 1) throw new Error('unique source timestamp required');
  return nodes[0];
}'''.replace('SCRIPT', mobile.MOBILE_SCAN_SCRIPT)


def copy_post_link(page, menu, *, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    source_url = page.url
    group = mobile._group_key(source_url)
    if not group or not group.isdecimal():
        raise mobile._layout_failure('numeric_source_group')
    def remaining():
        return mobile._remaining_seconds(deadline, 'timestamp_post_link') * 1000
    stamp = page.evaluate_handle(_TIMESTAMP, menu).as_element()
    if stamp is None:
        raise mobile._layout_failure('source_timestamp')
    try:
        # Dispatch to the timestamp: its containing header owns the action.
        stamp.dispatch_event('click')
        page.wait_for_url(lambda url: url != source_url, timeout=remaining())
        destination = urlsplit(page.url)
        query = parse_qs(destination.query)
        ids, groups = query.get('story_fbid', []), query.get('id', [])
        if (destination.scheme != 'https' or destination.hostname != 'm.facebook.com'
                or destination.path != '/story.php' or groups != [group]
                or len(ids) != 1 or not ids[0].isdecimal()):
            raise mobile._layout_failure('timestamp_post_in_source_group')
        return f'https://www.facebook.com/groups/{group}/posts/{ids[0]}/'
    finally:
        try:
            # Cleanup must still work if acquisition exhausted its deadline.
            cleanup_ms = 5000
            if page.url != source_url:
                page.go_back(wait_until='domcontentloaded', timeout=cleanup_ms)
                page.wait_for_url(source_url, timeout=cleanup_ms)
            if not menu.evaluate('n => n.isConnected'):
                raise mobile._layout_failure('original_feed_menu_restored')
        finally:
            stamp.dispose()

mobile.copy_post_link = copy_post_link
