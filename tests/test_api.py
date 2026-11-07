"""HTTP API checks with FastAPI's TestClient; no Facebook, no network.
Run: .venv/bin/python tests/test_api.py"""
import json
import sqlite3
import sys
import tempfile
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
def fetch_post_returns_schema_1_and_defaults_to_public_only():
    fake = Fake()
    r = client(fake).post("/fetch-post", json={"url": POST["url"]}, headers=AUTH)
    assert r.status_code == 200 and r.json() == POST, r.text
    api.FetchPostResponse.model_validate(r.json())
    kind, request, timeout = fake.calls[0]
    assert kind == "fetch" and request == {"url": POST["url"], "account": "default", "public_only": True}
    assert timeout == api.FETCH_TIMEOUT


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
        assert (r.headers.get("retry-after") == "30") == (kind == "profile_busy"), (kind, r.headers)


@case
def unknown_account_and_bad_body_are_400():
    c = client()
    r = c.post("/fetch-post", json={"url": POST["url"], "account": "nobody"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_account", r.text
    r = c.post("/fetch-post", json={"nope": 1}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text
    r = c.post("/fetch-post", content="not json", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text


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
    db.execute("INSERT INTO account_status VALUES ('default', 'ok', '', ?)", (int(time.time()),))
    db.execute("INSERT INTO account_status VALUES ('second', 'paused', 'AuthenticationRequiredError on account second', 1)")
    db.execute("INSERT INTO group_status VALUES ('default', '101', 'G', ?, '', 0, '')", (int(time.time()),))
    db.execute("INSERT INTO group_status VALUES ('second', '202', 'H', NULL, 'LayoutChangedError', 4, '10:00')")
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


for status, name, err in results:
    print(f"{status:4} {name}" + (f"  -- {err}" if err else ""))
print(f"{sum(s == 'pass' for s, _, _ in results)}/{len(results)} passed")
sys.exit(0 if all(s == "pass" for s, _, _ in results) else 1)
