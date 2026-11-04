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
