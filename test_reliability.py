"""Reliability regressions with fake browser/HTTP boundaries; no network."""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import braveduck_service as bds
import fbn_patch
import watcher as w
from fbn.models import Post
from playwright.sync_api import Error as PlaywrightError


def post(pid, pinned=False):
    return dict(post_id=pid, key=f"101:{pid}", text="same opening", author="A",
                url=f"https://www.facebook.com/groups/101/posts/{pid}/", is_pinned=pinned)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        with patch.object(w, "DB_PATH", ":memory:"):
            self.db = w.open_db()
        self.addCleanup(self.db.close)


class Storage(DatabaseTest):
    def test_fingerprint_expires_without_sliding_and_relearns_repost(self):
        with patch.object(w.time, "time", return_value=100):
            w.cache_posts(self.db, [post("1")], "101")
        with patch.object(w.time, "time", return_value=100 + 14 * 86400 - 1):
            w.cache_posts(self.db, [post("1")], "101")
            self.assertEqual(list(w.known_links(self.db, "101").values()), [post("1")["url"]])
        with patch.object(w.time, "time", return_value=100 + 14 * 86400):
            self.assertEqual(w.known_links(self.db, "101"), {})
            w.cache_posts(self.db, [post("2")], "101")
            self.assertEqual(list(w.known_links(self.db, "101").values()), [post("2")["url"]])
            self.assertEqual(w.known_links(self.db, "202"), {})

    def test_pinned_posts_never_become_boundary_even_after_a_gap(self):
        w.advance_boundary(self.db, "101", [post("1", True), post("2")], True)
        self.assertEqual(w.get_boundary(self.db, "101"), "2")
        for _ in range(3):
            w.advance_boundary(self.db, "101", [post("1", True), post("3")], False)
        self.assertEqual(w.get_boundary(self.db, "101"), "3")
        w.advance_boundary(self.db, "101", [post("1", True)], True)
        self.assertEqual(w.get_boundary(self.db, "101"), "3")


class Browser(unittest.TestCase):
    def test_launch_options_apply_with_and_without_proxy(self):
        for proxy in (None, {"server": "http://proxy.invalid:80"}):
            with self.subTest(proxy=proxy):
                browser = Mock()
                pw = SimpleNamespace(chromium=browser, devices={"Pixel 7": {}})
                source = w.make_source({"profile": Path("/fake"), "proxy": proxy}, True)
                with patch.object(w, "clear_stale_singleton"):
                    source._launch_context(pw, profile_dir=Path("/fake"), headless=True, timezone_id="UTC")
                options = browser.launch_persistent_context.call_args.kwargs
                self.assertEqual(options["timeout"], 60_000)
                self.assertIn("--disk-cache-size=52428800", options["args"])
                self.assertEqual(options.get("proxy"), proxy)

    def test_stale_cleanup_only_removes_dead_local_symlinks(self):
        host = os.uname().nodename
        for owner, probe, removed in [
            (f"{host}-12345", ProcessLookupError(), True),
            (f"{host}-12345", None, False),
            (f"{host}-12345", PermissionError(), False),
            ("other-host-12345", ProcessLookupError(), False),
            (f"{host}-bad", ProcessLookupError(), False),
            (f"{host}-0", ProcessLookupError(), False),
        ]:
            with self.subTest(owner=owner, probe=probe), \
                    patch.object(os, "readlink", return_value=owner), patch.object(os, "kill", side_effect=probe), \
                    patch.object(Path, "is_symlink", return_value=True), patch.object(Path, "unlink") as unlink:
                w.clear_stale_singleton(Path("/fake"))
                self.assertEqual(unlink.call_count, 3 if removed else 0)
        with patch.object(os, "readlink", side_effect=OSError), patch.object(Path, "unlink") as unlink:
            w.clear_stale_singleton(Path("/fake"))
            unlink.assert_not_called()

    def test_scan_worker_preserves_navigation_budget(self):
        request = dict(profile="/fake", proxy=None, group="101", sample_count=10, max_scrolls=4,
                       boundary=None, headless=True, navigation_timeout_seconds=20)
        policies = []
        def scan(account, group, policy, *args):
            policies.append(policy.navigation_timeout_seconds)
            return [], True
        with patch.object(w.sys, "stdin", io.StringIO(json.dumps(request))), \
                patch.object(w, "scan_in_process", side_effect=scan), contextlib.redirect_stdout(io.StringIO()):
            w.cmd_scan_worker()
        self.assertEqual(policies, [20])

    def test_check_worker_holds_lock_and_maps_browser_errors(self):
        held = []
        @contextlib.contextmanager
        def lock(profile):
            held.append(True)
            try:
                yield
            finally:
                held.pop()
        source = SimpleNamespace(_prepare_profile=lambda: Path("/fake"), _profile_lock=lock)
        request = dict(profile="/fake", proxy=None, group="101", headless=True)
        def check(*args):
            self.assertEqual(held, [True])
            raise PlaywrightError("private page detail")
        output = io.StringIO()
        with patch.object(w.sys, "stdin", io.StringIO(json.dumps(request))), \
                patch.object(w, "make_source", return_value=source), patch.object(w, "clear_stale_singleton"), \
                patch.object(bds, "check_group", side_effect=check), contextlib.redirect_stdout(output):
            w.cmd_check_worker()
        self.assertFalse(held)
        self.assertEqual(json.loads(output.getvalue()),
                         {"ok": False, "error": "TransientNavigationError", "message": "TransientNavigationError"})

    def test_check_parent_uses_worker_wall_and_error_mapping(self):
        account = dict(profile=Path("/fake"), proxy=None)
        for error in ("BrowserUnavailableError", "AuthenticationRequiredError", "ProfileInUseError", "Error"):
            with self.subTest(error=error), patch.object(w, "call_worker", return_value={
                    "ok": False, "error": error, "message": "failed"}) as worker:
                cls = getattr(w.fbn_exceptions, error, w.ScanWorkerError)
                with self.assertRaises(cls):
                    w.check_in_subprocess(account, "101")
                self.assertEqual(worker.call_args.args[0], "check-worker")
                self.assertEqual(worker.call_args.args[2], 240)
        with patch.object(w, "call_worker", side_effect=w.WorkerTimeout("hung")):
            with self.assertRaises(w.WorkerTimeout):
                w.check_in_subprocess(account, "101")

    def test_group_launch_failure_is_retryable_browser_error(self):
        pw = Mock()
        pw.chromium.launch_persistent_context.side_effect = PlaywrightError("slow launch")
        with patch("playwright.sync_api.sync_playwright") as manager:
            manager.return_value.__enter__.return_value = pw
            with self.assertRaises(w.BrowserUnavailableError):
                bds.load_group_page(Path("/fake"), None, "101")


class PinnedFeed(unittest.TestCase):
    def scan_fixture(self, pinned):
        mobile = fbn_patch.mobile
        stamp = datetime(2026, 11, 12, tzinfo=timezone.utc)
        source = w.make_source(dict(profile=Path("/fake"), proxy=None), True)
        page = SimpleNamespace(url="https://m.facebook.com/groups/101/", bring_to_front=lambda: None,
                               evaluate=lambda *args: None)
        passes = iter(range(4))
        @contextlib.contextmanager
        def records(page):
            i = next(passes)
            yield [(str(i), dict(cardToken=i, actionToken=i, text=str(i), author="A", isPinned=pinned and i == 0))]
        def extract(payloads, group, observed, *args, **kwargs):
            pid = payloads[0]["text"]
            return [Post(group.key, pid, post(pid)["url"], pid, "A", stamp, 0, published_at=stamp)]
        fbn_patch.pinned_post_ids.clear()
        self.addCleanup(fbn_patch.pinned_post_ids.clear)
        with contextlib.ExitStack() as stack:
            for name, value in dict(mobile_records=records, wait_for_mobile_page=lambda *a, **k: None,
                                    _post_signature=lambda p: (p["text"],),
                                    copy_post_link=lambda page, menu, **k: menu,
                                    _post_target=lambda *a, **k: contextlib.nullcontext(None),
                                    resolve_post_link=lambda target, copied, *a, **k: copied,
                                    extract_posts=extract, _publication_label=lambda p: "",
                                    parse_facebook_timestamp=lambda *a, **k: stamp).items():
                stack.enter_context(patch.object(mobile, name, value))
            stack.enter_context(patch.object(source, "_wait_for_scroll_progress"))
            stack.enter_context(patch.object(w, "make_source", return_value=source))
            def fetch(group, policy, boundary_post_id):
                return source._scan_mobile(page, None, group, policy, frozenset({"101"}), boundary_post_id)
            stack.enter_context(patch.object(source, "fetch_recent", side_effect=fetch))
            posts, complete = w.scan_in_process(dict(profile=Path("/fake"), proxy=None), w.parse_group_ref("101"),
                                                w.ScanPolicy(max_scrolls=3), "0")
        return posts, complete

    def test_old_pinned_boundary_does_not_stop_before_later_scrolls(self):
        posts, complete = self.scan_fixture(True)
        self.assertEqual({p["post_id"] for p in posts}, {"0", "1", "2", "3"})
        self.assertTrue(next(p for p in posts if p["post_id"] == "0")["is_pinned"])
        self.assertFalse(complete)

    def test_unpinned_boundary_still_stops_after_one_overlap_pass(self):
        posts, complete = self.scan_fixture(False)
        self.assertEqual({p["post_id"] for p in posts}, {"0", "1"})
        self.assertTrue(complete)


class HealthClient(unittest.TestCase):
    def test_health_body_and_404_feature_detection_without_network(self):
        client = bds.ScannerAPI("https://scanner.invalid", "fake")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        with patch.object(bds.urllib.request, "urlopen", return_value=response) as send:
            client.health(False, "WorkerTimeout on 101")
            req = send.call_args.args[0]
            self.assertEqual(req.full_url, "https://scanner.invalid/health")
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.get_header("Authorization"), "Bearer fake")
            self.assertEqual(json.loads(req.data), {"ok": False, "error": "WorkerTimeout on 101"})
        with patch.object(bds.urllib.request, "urlopen", side_effect=HTTPError("", 404, "", {}, None)) as send:
            client.health(True)
            client.health(False, "failed")
            self.assertEqual(send.call_count, 1)

    def test_non404_is_not_hidden_or_cached(self):
        client = bds.ScannerAPI("https://scanner.invalid", "fake")
        with patch.object(bds.urllib.request, "urlopen", side_effect=HTTPError("", 503, "", {}, None)) as send:
            for _ in range(2):
                with self.assertRaises(HTTPError):
                    client.health(False, "failed")
            self.assertEqual(send.call_count, 2)


class StopLoop(Exception):
    pass


class Serve(DatabaseTest):
    def run_rounds(self, outcomes, *, group_error=None, check_error=None, delivery_error=None,
                   groups=None, rounds=None):
        # Each /health call is the round boundary, including failure and exit rounds.
        snapshots, sleeps, health = [], [], []
        cfg = dict(poll_seconds=[90, 90], sample_count=10, scrolls=4)
        account = dict(name="default", profile=Path("/fake"), proxy=None)
        api = Mock()
        api.groups.side_effect = group_error
        api.groups.return_value = groups if groups is not None else [
            dict(key="101", visibility="checking" if check_error else "public")]
        api.posts.side_effect = delivery_error
        def report(ok, reason):
            health.append((ok, reason))
            snapshots.append((self.db.execute("SELECT state, reason, last_round FROM account_status").fetchone(),
                              self.db.execute("SELECT last_ok, error, failed_rounds FROM group_status").fetchone()))
        api.health.side_effect = report
        def sleep(seconds):
            sleeps.append(seconds)
            if len(health) >= (rounds or len(outcomes)):
                raise StopLoop()
        with contextlib.ExitStack() as stack:
            for obj, name, value in [(w, "load_config", lambda: cfg), (w, "braveduck_api", lambda cfg: api),
                                     (w, "load_accounts", lambda cfg: [account]), (w, "open_db", lambda: self.db),
                                     (w.time, "sleep", sleep), (bds, "due_for_check", lambda g: bool(check_error))]:
                stack.enter_context(patch.object(obj, name, value))
            scans = stack.enter_context(patch.object(w, "run_scan", side_effect=outcomes))
            checks = stack.enter_context(patch.object(w, "check_in_subprocess", side_effect=check_error))
            alerts = stack.enter_context(patch.object(w, "send_telegram"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = None
            try:
                w.cmd_serve_braveduck()
            except StopLoop:
                pass
            except SystemExit as e:
                code = e.code
        return SimpleNamespace(health=health, snapshots=snapshots, sleeps=sleeps, code=code,
                               scans=scans, checks=checks, alerts=alerts, api=api)

    def test_browser_backoff_then_exit_75_with_status_written_first(self):
        r = self.run_rounds([w.BrowserUnavailableError("launch")] * 5)
        self.assertEqual(r.sleeps, [120, 300, 600, 1200])
        self.assertEqual(r.code, 75)
        self.assertEqual([s[1][2] for s in r.snapshots], [1, 2, 3, 4, 5])
        self.assertTrue(all(not ok and "BrowserUnavailableError" in reason for ok, reason in r.health))
        r.alerts.assert_not_called()

    def test_success_resets_launch_streak_and_group_failure(self):
        r = self.run_rounds([w.BrowserUnavailableError("launch"), ([], True),
                             w.BrowserUnavailableError("launch")])
        self.assertEqual([s for s in r.sleeps if s > 90], [120, 120])
        self.assertEqual([ok for ok, _ in r.health], [False, True, False])
        self.assertEqual([s[0][0] for s in r.snapshots], ["unknown", "ok", "unknown"])
        self.assertEqual([s[1][2] for s in r.snapshots], [1, 0, 1])
        self.assertIsNotNone(r.snapshots[2][1][0])  # last success survives a failed round
        self.assertEqual(r.scans.call_args.args[2].navigation_timeout_seconds, 20)

    def test_group_check_errors_stay_in_loop_and_prevent_scan(self):
        r = self.run_rounds([None], check_error=w.fbn_exceptions.TransientNavigationError("page"))
        r.scans.assert_not_called()
        self.assertFalse(r.health[0][0])
        self.assertEqual(r.snapshots[0][1][1], "TransientNavigationError")
        self.assertIsNone(r.code)

    def test_login_failure_records_paused_before_exit_3(self):
        r = self.run_rounds([w.AuthenticationRequiredError("login")])
        self.assertEqual(r.code, 3)
        self.assertEqual(r.snapshots[0][0][0], "paused")
        self.assertFalse(r.health[0][0])
        r.alerts.assert_called_once()

    def test_groups_api_failure_updates_local_round(self):
        r = self.run_rounds([None], group_error=OSError("unreachable"))
        r.scans.assert_not_called()
        self.assertEqual(r.health, [(False, "OSError fetching groups")])
        self.assertEqual(r.snapshots[0][0][0], "unknown")
        self.assertIsNotNone(r.snapshots[0][0][2])

    def test_delivery_failure_keeps_post_pending_and_health_failed(self):
        r = self.run_rounds([([post("1")], True)], delivery_error=OSError("offline"))
        self.assertFalse(w.is_seen(self.db, "101:1"))
        self.assertFalse(r.health[0][0])
        self.assertEqual(r.snapshots[0][1][1], "OSError")

    def test_one_success_cannot_hide_another_groups_timeout(self):
        groups = [dict(key="101", visibility="public"), dict(key="202", visibility="public")]
        r = self.run_rounds([([], True), w.WorkerTimeout("hung")], groups=groups, rounds=1)
        self.assertEqual(r.health, [(False, "WorkerTimeout on 202")])
        self.assertEqual(self.db.execute("SELECT failed_rounds FROM group_status WHERE group_id='202'").fetchone(), (1,))

    def test_no_scan_is_not_reported_as_success(self):
        r = self.run_rounds([None], groups=[])
        self.assertEqual(r.health, [(False, "No public groups scanned")])
        self.assertEqual(r.snapshots[0][0][0], "unknown")


if __name__ == "__main__":
    unittest.main()
