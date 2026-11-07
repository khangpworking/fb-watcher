"""Run loop, outbox, boundary and scan-worker checks. No Facebook. Run: .venv/bin/python tests/test_run_loop.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ["FB_WATCHER_INPROCESS"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import watcher as w  # noqa: E402

TMP = Path(tempfile.mkdtemp())
results = []


def case(fn):
    try:
        fn()
        results.append(("pass", fn.__name__, ""))
    except Exception as e:
        results.append(("FAIL", fn.__name__, f"{type(e).__name__}: {e}"))


class Stop(Exception):
    pass


def post(i, gid="101"):
    return {"post_id": str(i), "key": f"{gid}:{i}", "text": f"AI job {i}", "url": f"https://www.facebook.com/groups/{gid}/posts/{i}/", "author": "A"}


def run(db_name, scans, rounds, add_results=None, boundaries=None):
    """Run cmd_run with scripted run_scan results; scans[gid] is a list of ([posts], complete) or exceptions."""
    path = TMP / db_name
    adds, health, calls = [], [], []
    add_results = iter(add_results or [])
    idx = {}

    def fake_scan(account, ref, policy, boundary, headless=True, known=None):
        i = idx.get(ref.key, 0)
        idx[ref.key] = i + 1
        calls.append((ref.key, boundary))
        value = scans[ref.key][min(i, len(scans[ref.key]) - 1)]
        if isinstance(value, Exception):
            raise value
        posts, complete = value
        return posts, complete

    def fake_add(cfg, p, gid, name, hits):
        adds.append(p["key"])
        return next(add_results, True)

    def report(cfg, ok, reason=""):
        health.append((ok, reason))
        if len(health) >= rounds:
            raise Stop

    cfg = dict(groups=[dict(id=g, name="G" + g) for g in scans], keywords=["AI"], poll_seconds=[0, 0],
               sample_count=10, scrolls=4, telegram_token="", telegram_chat_id="", braveduck=dict(enabled=True))
    with contextlib.ExitStack() as st:
        for obj, key, value in [(w, "DB_PATH", path), (w, "load_config", lambda: cfg), (w, "run_scan", fake_scan),
                                (w, "add_to_braveduck", fake_add), (w, "report_to_braveduck", report),
                                (w, "send_telegram", lambda cfg, m: True), (w.time, "sleep", lambda s: None)]:
            st.enter_context(patch.object(obj, key, value))
        st.enter_context(contextlib.redirect_stdout(io.StringIO()))
        try:
            w.cmd_run()
        except Stop:
            pass
    db = w.sqlite3.connect(path)
    outbox = db.execute("SELECT post_key, sink, attempts FROM outbox ORDER BY post_key, sink").fetchall()
    boundary = dict(db.execute("SELECT * FROM boundary").fetchall())
    return dict(adds=adds, health=health, calls=calls, outbox=outbox, boundary=boundary)


@case
def bootstrap_then_new_post_delivered_once_across_restart():
    a = run("dedup.db", {"101": [([post(1)], True), ([post(2), post(1)], True)]}, 2)
    b = run("dedup.db", {"101": [([post(2), post(1)], True), ([post(3), post(2)], True)]}, 2)
    assert a["adds"] == ["101:2"] and b["adds"] == ["101:3"], (a["adds"], b["adds"])
    assert b["outbox"] == [] and b["boundary"] == {"101": "3"}


@case
def failed_add_is_retried_after_restart_and_reported():
    # Post 2's add fails 3 times (rounds 2-4), then the service restarts and it succeeds.
    a = run("retry.db", {"101": [([post(1)], True), ([post(2), post(1)], True)]}, 4, add_results=[False, False, False])
    assert a["adds"] == ["101:2"] * 3, a["adds"]
    assert ("101:2", "braveduck", 3) in a["outbox"], a["outbox"]
    assert a["health"][-1] == (False, "1 of 1 deliveries failing"), a["health"]
    b = run("retry.db", {"101": [([post(2), post(1)], True)]}, 1)
    assert b["adds"] == ["101:2"] and b["outbox"] == [] and b["health"][-1][0], (b["adds"], b["outbox"], b["health"])


@case
def telegram_and_braveduck_retried_independently():
    sent = []
    flaky = iter([False, True])
    with patch.object(w, "send_telegram", lambda cfg, m: sent.append(m) or next(flaky, True)):
        db = w.sqlite3.connect(TMP / "indep.db")
        w.DB_PATH = TMP / "indep.db"
        db = w.open_db()
        w.record_posts(db, [post(5)], "101", "G", False, w.build_matcher(["AI"]))
        with patch.object(w, "add_to_braveduck", lambda *a: True), contextlib.redirect_stdout(io.StringIO()):
            first = w.deliver_pending({}, db)
            second = w.deliver_pending({}, db)
    assert first == (1, 0) and second == (0, 0) and len(sent) == 2, (first, second, sent)


@case
def incomplete_scan_keeps_boundary_then_reports_gap():
    # Boundary 1 after round 1; then three scans that never reach it, then one that does.
    r = run("gap.db", {"101": [([post(1)], True), ([post(5), post(4)], False), ([post(6), post(5)], False),
                               ([post(7), post(6)], False), ([post(8), post(7)], True)]}, 5)
    boundaries = [b for _, b in r["calls"]]
    assert boundaries == [None, "1", "1", "1", "7"], boundaries
    assert r["health"][3] == (False, "incomplete scans on G101 (101) for 3 rounds"), r["health"]
    assert r["health"][4] == (True, ""), r["health"]
    assert r["boundary"] == {"101": "8"}


@case
def posts_from_incomplete_scan_still_delivered():
    r = run("partial.db", {"101": [([post(1)], True), ([post(3)], False)]}, 2)
    assert r["adds"] == ["101:3"], r["adds"]


@case
def worker_hang_is_killed_and_group_skipped():
    # A fake worker that never answers: the parent must kill its process group in time.
    fake = TMP / "hang_worker.py"
    fake.write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\ntime.sleep(600)\n")
    real_popen = subprocess.Popen

    def popen(args, **kw):
        return real_popen([sys.executable, str(fake)], **kw)

    with patch.object(w, "SCAN_WALL_SECONDS", 2), patch.object(w.subprocess, "Popen", popen):
        start = time.monotonic()
        try:
            w.scan_in_subprocess({"profile": TMP, "proxy": None}, w.parse_group_ref("101"), w.ScanPolicy(), None)
            raise AssertionError("no error raised")
        except w.ScanWorkerError as e:
            took = time.monotonic() - start
    assert took < 6, took
    left = subprocess.run(["pgrep", "-f", "time.sleep\\(600\\)"], capture_output=True, text=True).stdout.split()
    assert not left, f"worker children still running: {left}"


@case
def worker_error_is_raised_as_same_fbn_class():
    fake = TMP / "err_worker.py"
    fake.write_text('import json; print(json.dumps({"ok": False, "error": "AuthenticationRequiredError", "message": "not signed in"}))\n')
    real_popen = subprocess.Popen
    with patch.object(w.subprocess, "Popen", lambda args, **kw: real_popen([sys.executable, str(fake)], **kw)):
        try:
            w.scan_in_subprocess({"profile": TMP, "proxy": None}, w.parse_group_ref("101"), w.ScanPolicy(), None)
        except w.AuthenticationRequiredError as e:
            assert str(e) == "not signed in"
            return
    raise AssertionError("expected AuthenticationRequiredError")


@case
def real_worker_round_trip_returns_fbn_error():
    # The real `watcher.py scan-worker` process. Its profile path is a file, so fbn refuses it
    # before any browser starts: no Facebook.
    bad_profile = TMP / "not-a-dir"
    bad_profile.write_text("x")
    try:
        w.scan_in_subprocess({"profile": bad_profile, "proxy": None}, w.parse_group_ref("101"),
                             w.ScanPolicy(sample_count=1, max_scrolls=0), None)
    except w.ConfigurationError:
        return
    raise AssertionError("expected ConfigurationError from a real worker")


for status, name, err in results:
    print(f"{status:4} {name}" + (f"  -- {err}" if err else ""))
print(f"{sum(s == 'pass' for s, _, _ in results)}/{len(results)} passed")
sys.exit(0 if all(s == "pass" for s, _, _ in results) else 1)
