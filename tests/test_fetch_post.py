"""fetch-post checks without Facebook. Run: .venv/bin/python tests/test_fetch_post.py"""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_post as fp  # noqa: E402
import watcher  # noqa: E402

results = []


def case(fn):
    try:
        fn()
        results.append(("pass", fn.__name__, ""))
    except Exception as e:
        results.append(("FAIL", fn.__name__, f"{type(e).__name__}: {e}"))


def cli(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fp.main(list(args))
    return code, out.getvalue()


@case
def accepts_group_post_and_share_links():
    ok = ["https://www.facebook.com/groups/123/posts/456/", "https://m.facebook.com/groups/123/permalink/456/",
          "https://web.facebook.com/groups/123/posts/456/", "https://www.facebook.com/share/p/19fpD6jRRA/"]
    bad = ["https://example.com/groups/123/posts/456/", "http://www.facebook.com/groups/123/posts/456/",
           "https://www.facebook.com/groups/123/", "https://www.facebook.com/share/p/x/?y=1#z", "not a url"]
    assert all(fp._supported_url(u) for u in ok), [u for u in ok if not fp._supported_url(u)]
    assert not any(fp._supported_url(u) for u in bad), [u for u in bad if fp._supported_url(u)]


@case
def mobile_links_are_read_on_the_desktop_site():
    assert fp._desktop_url("https://m.facebook.com/groups/1/posts/2/") == "https://www.facebook.com/groups/1/posts/2/"


@case
def bad_url_and_usage_exit_2_with_json_error():
    code, out = cli("https://example.com/x", "--json")
    assert code == fp.EXIT_USAGE and json.loads(out) == {
        "schema": 1, "error": "bad_url", "message": "not a Facebook group post or share link"}, (code, out)
    assert cli()[0] == fp.EXIT_USAGE and cli("u", "a", "b")[0] == fp.EXIT_USAGE
    assert cli("https://www.facebook.com/groups/1/posts/2/", "--assets-dir")[0] == fp.EXIT_USAGE


@case
def unknown_account_exits_2():
    with patch.object(watcher, "load_config", lambda: {"groups": [], "keywords": ["x"]}):
        code, out = cli("https://www.facebook.com/groups/1/posts/2/", "nobody", "--json")
    assert code == fp.EXIT_USAGE and json.loads(out)["error"] == "bad_account", (code, out)


@case
def busy_profile_waits_then_exits_5():
    class Busy:
        def _prepare_profile(self):
            return Path("/nonexistent")

        @contextlib.contextmanager
        def _profile_lock(self, _):
            raise fp.ProfileInUseError("busy")
            yield

    acct = {"name": "t", "profile": Path("/nonexistent"), "proxy": None}
    with patch.object(watcher, "make_source", lambda a, h: Busy()), patch.object(fp.time, "sleep", lambda s: None):
        try:
            fp.fetch_post("https://www.facebook.com/groups/1/posts/2/", acct, lock_wait=0)
        except fp.FetchError as e:
            assert e.code == fp.EXIT_BUSY and e.kind == "profile_busy", (e.code, e.kind)
            return
    raise AssertionError("expected FetchError(profile_busy)")


@case
def exit_codes_are_distinct():
    codes = [fp.EXIT_OK, fp.EXIT_ERROR, fp.EXIT_USAGE, fp.EXIT_LOGIN, fp.EXIT_NOT_VISIBLE, fp.EXIT_BUSY]
    assert len(set(codes)) == len(codes) and fp.EXIT_LOGIN == watcher.EXIT_NEEDS_USER


for status, name, err in results:
    print(f"{status:4} {name}" + (f"  -- {err}" if err else ""))
print(f"{sum(s == 'pass' for s, _, _ in results)}/{len(results)} passed")
sys.exit(0 if all(s == "pass" for s, _, _ in results) else 1)
