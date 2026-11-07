"""HTTP API checks with FastAPI's TestClient; no Facebook, no network.
Run: .venv/bin/python tests/test_api.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import watcher  # noqa: E402

KEY = "test-key-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {KEY}"}
POST = {"schema": 1, "url": "https://www.facebook.com/groups/1/posts/2/", "post_id": "2", "group_id": "1",
        "group_key": "1", "group_name": "G", "author": "A", "text": "AI job", "published_at": None,
        "published_label": None, "images": [], "image_paths": [], "fetched_at": "2026-11-07T00:00:00+00:00"}
GROUP = {"name": "G", "visibility": "public", "note": "", "group_id": "1"}
TMP = Path(tempfile.mkdtemp())
results = []


def case(fn):
    try:
        fn()
        results.append(("pass", fn.__name__, ""))
    except Exception as e:
        results.append(("FAIL", fn.__name__, f"{type(e).__name__}: {e}"))


class Fake:
    """Stands in for call_browser: returns results or raises ApiError, and records calls."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, kind, request, timeout):
        self.calls.append((kind, request, timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else (POST if kind == "fetch" else GROUP)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(browser=None, **kw):
    kw.setdefault("account_names", ["default", "second"])
    kw.setdefault("db_path", TMP / "none.db")
    return TestClient(api.create_app(key=KEY, browser=browser or Fake(), **kw))


@case
def healthz_needs_no_key_and_says_nothing_else():
    r = client().get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text


@case
def auth_missing_wrong_and_right_key():
    c = client()
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}, {"Authorization": f"Basic {KEY}"}):
        r = c.get("/status", headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", (headers, r.status_code, r.text)
        assert r.headers.get("www-authenticate") == "Bearer"
    assert c.get("/status", headers=AUTH).status_code == 200
    assert c.post("/fetch-post", json={"url": POST["url"]}).status_code == 401


@case
def fetch_post_returns_schema_1():
    fake = Fake()
    r = client(fake).post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert r.status_code == 200 and r.json() == POST, r.text
    api.FetchPostResponse.model_validate(r.json())
    kind, request, timeout = fake.calls[0]
    assert kind == "fetch" and request == {"url": POST["url"], "account": "default"}
    assert timeout == api.FETCH_TIMEOUT


@case
def callers_cant_turn_off_public_only():
    fake = Fake()
    r = client(fake).post("/fetch-post", json={"url": POST["url"], "public_only": False}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_request" and not fake.calls, r.text
    calls = []
    with patch("fetch_post.fetch_post", side_effect=lambda *a, **k: calls.append(k) or POST), \
            patch.object(sys, "stdin", type("I", (), {"read": lambda self: json.dumps({"kind": "fetch", "url": POST["url"], "public_only": False})})()):
        api.cmd_api_worker()
    assert calls and calls[0]["public_only"] is True, calls


@case
def check_group_returns_contract():
    fake = Fake()
    r = client(fake).post("/check-group", json={"url": "https://www.facebook.com/groups/1/", "account": "second"},
                          headers=AUTH)
    assert r.status_code == 200 and r.json() == GROUP, r.text
    assert fake.calls[0][1]["account"] == "second" and fake.calls[0][2] == api.CHECK_TIMEOUT


@case
def each_error_kind_maps_to_its_status_with_error_body():
    for kind, status in [("bad_url", 400), ("bad_account", 400), ("not_public", 403), ("not_visible", 404),
                         ("error", 502), ("login_required", 503), ("profile_busy", 503), ("timeout", 504)]:
        retry = 30 if kind == "profile_busy" else None
        r = client(Fake(api.ApiError(kind, "m", retry))).post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
        assert r.status_code == status, (kind, r.status_code)
        assert r.json() == {"schema": 1, "error": kind, "message": "m"}, (kind, r.text)
        assert ("retry-after" in r.headers) == (kind in ("profile_busy", "login_required")), (kind, r.headers)


@case
def unknown_account_and_bad_body_are_400():
    c = client()
    r = c.post("/fetch-post", json={"url": POST["url"], "account": "nobody"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_account", r.text
    r = c.post("/fetch-post", json={"nope": 1}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text
    for body in (b"not json", b"\xff\xfe{", b'{"url": 5}'):
        r = c.post("/fetch-post", content=body, headers={**AUTH, "Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["schema"] == 1, (body, r.status_code, r.text)
    r = c.post("/fetch-post", json={"url": "x" * 3000}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text


@case
def unknown_routes_methods_and_big_bodies_use_the_error_shape():
    c = client()
    r = c.get("/nope", headers=AUTH)
    assert r.status_code == 404 and r.json()["error"] == "not_found", r.text
    r = c.get("/fetch-post", headers=AUTH)
    assert r.status_code == 405 and r.json()["error"] == "method_not_allowed" and "allow" in r.headers, r.text
    big = json.dumps({"url": POST["url"], "account": "x" * 20000})
    r = c.post("/fetch-post", content=big, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large", r.text
    r = c.post("/fetch-post", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 401, "anonymous requests are refused before the body is read"


@case
def same_post_linked_differently_is_fetched_once():
    fake = Fake()
    c = client(fake)
    for url in (POST["url"], "https://m.facebook.com/groups/1/permalink/2/?__cft__=x",
                "https://web.facebook.com/groups/1/posts/2/#c"):
        assert c.post("/fetch-post", json={"url": url}, headers=AUTH).status_code == 200
    assert len(fake.calls) == 1, fake.calls


@case
def concurrent_identical_requests_run_one_browser_job():
    started = []

    def slow(kind, request, timeout):
        started.append(time.monotonic())
        time.sleep(0.5)
        return POST
    c = client(slow)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(
        c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH).status_code)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * 4 and len(started) == 1, (codes, started)


@case
def paused_account_and_login_problem_stop_further_calls():
    db_path = TMP / "paused.db"
    with patch.object(watcher, "DB_PATH", db_path):
        db = watcher.open_db()
        watcher.write_status(db, [{"name": "second"}], {"second": "AccountActionRequiredError on account second"}, {})
    fake = Fake()
    r = client(fake, db_path=db_path).post("/fetch-post", json={"url": POST["url"], "account": "second"}, headers=AUTH)
    assert r.status_code == 503 and r.json()["error"] == "login_required" and "retry-after" in r.headers, r.text
    assert not fake.calls
    now = [0.0]
    fake = Fake(api.ApiError("login_required", "logged out"))
    c = client(fake, clock=lambda: now[0], cache_seconds=0)
    assert c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH).status_code == 503
    r = c.post("/fetch-post", json={"url": POST["url"] + "?x"}, headers=AUTH)
    assert r.status_code == 503 and len(fake.calls) == 1, (r.text, fake.calls)
    now[0] = api.LOGIN_RETRY_AFTER_SECONDS + 1
    assert c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH).status_code == 200


@case
def private_check_drops_cached_posts_of_that_group():
    fake = Fake(POST, {"name": "G", "visibility": "private", "note": "", "group_id": "1"}, POST)
    c = client(fake)
    c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    c.post("/check-group", json={"url": "1"}, headers=AUTH)
    c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert [k for k, _, _ in fake.calls] == ["fetch", "check", "fetch"], fake.calls


@case
def unexpected_worker_result_is_502_and_never_logged():
    lines = []
    handler = type("H", (api.logging.Handler,), {"emit": lambda self, rec: lines.append(rec.getMessage())})()
    root = api.logging.getLogger()
    root.addHandler(handler)
    try:
        bad = {k: v for k, v in POST.items() if k != "group_id"} | {"text": "PRIVATE TEXT"}
        r = client(Fake(bad)).post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    finally:
        root.removeHandler(handler)
    assert r.status_code == 502 and r.json()["error"] == "error" and "PRIVATE" not in r.text, r.text
    assert not any("PRIVATE TEXT" in line for line in lines), lines


@case
def repeated_fetch_is_cached_and_doesnt_hit_facebook():
    now = [0.0]
    fake = Fake()
    c = client(fake, clock=lambda: now[0], cache_seconds=600)
    for _ in range(3):
        assert c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH).status_code == 200
    assert len(fake.calls) == 1, fake.calls
    now[0] = 601
    c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert len(fake.calls) == 2


@case
def rate_limit_per_account_gives_429_with_retry_after():
    now = [0.0]
    c = client(Fake(), clock=lambda: now[0], calls_per_hour=2, cache_seconds=0)
    codes = [c.post("/fetch-post", json={"url": POST["url"] + f"?x={i}"}, headers=AUTH).status_code for i in range(3)]
    assert codes == [200, 200, 429], codes
    r = c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert r.json()["error"] == "rate_limited" and int(r.headers["retry-after"]) > 0
    assert c.post("/fetch-post", json={"url": POST["url"], "account": "second"}, headers=AUTH).status_code == 200
    now[0] = 3601
    assert c.post("/fetch-post", json={"url": POST["url"]}, headers=AUTH).status_code == 200


@case
def worker_timeout_and_busy_replies_are_mapped():
    with patch.object(watcher, "call_worker", side_effect=watcher.WorkerTimeout("slow")):
        r = client(api.call_browser).post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert r.status_code == 504 and r.json()["error"] == "timeout", r.text
    busy = {"ok": False, "error": "profile_busy", "message": "account profile still busy after 15s"}
    with patch.object(watcher, "call_worker", return_value=busy):
        r = client(api.call_browser).post("/check-group", json={"url": "1"}, headers=AUTH)
    assert r.status_code == 503 and r.headers["retry-after"] == str(api.RETRY_AFTER_SECONDS), (r.status_code, r.headers)


@case
def real_worker_round_trip_rejects_bad_url_without_facebook():
    r = client(api.call_browser).post("/fetch-post", json={"url": "https://example.com/x"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_url", r.text


@case
def status_reports_health_and_leaks_no_secrets():
    db_path = TMP / "status.db"
    with patch.object(watcher, "DB_PATH", db_path):
        db = watcher.open_db()
    db.execute("INSERT INTO group_status VALUES ('default', '101', 'G', ?, '', 0, '')", (int(time.time()),))
    watcher.write_status(db, [{"name": "default"}, {"name": "second"}],
                         {"second": "AuthenticationRequiredError on account second"},
                         {("second", "202"): ("H", "10:00", 4, "LayoutChangedError")})
    db.execute("INSERT INTO group_state (group_id, initialized, incomplete) VALUES ('101', 1, 2)")
    secret_post = {"post": {"text": "SECRET POST TEXT", "url": "https://x/?__cft__=TRACK"}}
    db.execute("INSERT INTO outbox VALUES ('101:9', 'braveduck', ?, 5, 0)", (json.dumps(secret_post),))
    db.execute("INSERT INTO seen VALUES ('101:9', '101', 0)")
    db.commit()
    r = client(db_path=db_path).get("/status", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    api.StatusResponse.model_validate(body)
    assert [a["state"] for a in body["accounts"]] == ["ok", "paused"], body
    g = {x["group_id"]: x for x in body["groups"]}
    assert g["101"]["incomplete_rounds"] == 2 and g["202"]["failed_rounds"] == 4, g
    assert body["deliveries"] == {"pending": 1, "failing": 1}, body
    for secret in ("SECRET POST TEXT", "__cft__", "TRACK", KEY, "secret", "pass"):
        assert secret not in r.text, secret


@case
def status_without_a_database_says_unknown():
    r = client(db_path=TMP / "missing.db").get("/status", headers=AUTH)
    assert r.status_code == 200 and {a["state"] for a in r.json()["accounts"]} == {"unknown"}, r.text


@case
def docs_off_unless_asked_and_openapi_documents_contract():
    assert client().get("/docs").status_code == 404 and client().get("/redoc").status_code == 404
    assert client(docs=True).get("/docs").status_code == 200
    spec = client().get("/openapi.json").json()
    assert set(spec["paths"]) == {"/healthz", "/fetch-post", "/check-group", "/status"}, spec["paths"].keys()
    props = spec["components"]["schemas"]["FetchPostResponse"]["properties"]
    assert "schema" in props and "text" in props
    op = spec["paths"]["/fetch-post"]["post"]
    assert op["security"] == [{"HTTPBearer": []}] and "422" not in op["responses"], op
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert "public_only" not in spec["components"]["schemas"]["FetchPostRequest"]["properties"]


@case
def refuses_to_start_without_a_key():
    try:
        api.create_app(key="", account_names=["default"])
        raise AssertionError("created an app without a key")
    except ValueError:
        pass
    for cfg, env in [({}, {}), ({"api": {"key_file": str(TMP / "nope")}}, {})]:
        with patch.dict("os.environ", env, clear=False):
            try:
                api.load_key(cfg)
                raise AssertionError(f"accepted {cfg}")
            except SystemExit:
                pass
    short = TMP / "short.key"
    short.write_text("abc")
    try:
        api.load_key({"api": {"key_file": str(short)}})
        raise AssertionError("accepted a short key")
    except SystemExit:
        pass
    good = TMP / "good.key"
    good.write_text(KEY + "\n")
    assert api.load_key({"api": {"key_file": str(good)}}) == KEY


@case
def logs_have_no_body_or_query():
    lines = []
    handler = type("H", (api.logging.Handler,), {"emit": lambda self, rec: lines.append(rec.getMessage())})()
    api.log.addHandler(handler)
    api.log.setLevel(api.logging.INFO)
    try:
        client().post("/fetch-post?token=QUERYSECRET", json={"url": POST["url"] + "?__cft__=X"}, headers=AUTH)
    finally:
        api.log.removeHandler(handler)
    assert lines and lines[-1].startswith("POST /fetch-post 200"), lines
    assert not any(s in " ".join(lines) for s in ("QUERYSECRET", "__cft__", KEY, "AI job")), lines


@case
def timed_out_worker_leaves_no_chromium_behind():
    profile = TMP / "kill-profile"
    script = TMP / "hang_browser.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(watcher.ROOT)!r})\n"
        "import watcher\n"
        "from playwright.sync_api import sync_playwright\n"
        "p = sync_playwright().start()\n"
        f"p.chromium.launch_persistent_context({str(profile)!r}, headless=True)\n"
        "time.sleep(600)\n")
    real = subprocess.Popen
    with patch.object(watcher.subprocess, "Popen", lambda args, **kw: real([sys.executable, str(script)], **kw)):
        try:
            watcher.call_worker("api-worker", {}, 8)
            raise AssertionError("no timeout")
        except watcher.WorkerTimeout:
            pass
    time.sleep(1)
    left = subprocess.run(["pgrep", "-f", str(profile)], capture_output=True, text=True).stdout.split()
    assert not left, f"Chromium still running: {left}"


for status, name, err in results:
    print(f"{status:4} {name}" + (f"  -- {err}" if err else ""))
print(f"{sum(s == 'pass' for s, _, _ in results)}/{len(results)} passed")
sys.exit(0 if all(s == "pass" for s, _, _ in results) else 1)
