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
