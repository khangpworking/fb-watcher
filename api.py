"""fb-watcher's HTTP API: fetch-post and check-group for other services (braveduck's Go
server, in another container), plus health and status. Run: `python watcher.py api`.

- Only posts from confirmed public groups are returned; callers can't turn that off.
- Every browser call runs in its own worker process (`watcher.py api-worker`), one at a time
  per account, takes the account's profile lock (waiting at most LOCK_WAIT_SECONDS) and is
  killed with its browser at its time limit.
- Calls are rate-limited per account, identical requests are answered from a short cache,
  and accounts the watcher has paused (or that just hit a login problem) are refused, so
  callers can't drive Facebook page loads.
See README "HTTP API".
"""

import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import watcher

SCHEMA = 1
LOCK_WAIT_SECONDS = 15
FETCH_TIMEOUT = 120
CHECK_TIMEOUT = 90
RETRY_AFTER_SECONDS = 30
LOGIN_RETRY_AFTER_SECONDS = 1800  # how long an account rests after the API hits a login problem
MAX_BODY_BYTES = 16 * 1024
DEFAULT_CALLS_PER_HOUR = 30
DEFAULT_CACHE_SECONDS = 600

log = logging.getLogger("fb_watcher.api")

# error kind -> HTTP status. The body is always ErrorResponse.
STATUS_FOR = {
    "bad_url": 400, "bad_account": 400, "bad_request": 400,
    "unauthorized": 401,
    "not_found": 404, "method_not_allowed": 405, "length_required": 411, "too_large": 413,
    "not_public": 403,
    "not_visible": 404,
    "rate_limited": 429,
    "error": 502,
    "login_required": 503, "profile_busy": 503,
    "timeout": 504,
}


# --- contract -----------------------------------------------------------------

class FetchPostRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(max_length=2048, description="Facebook group post or share link")
    account: Optional[str] = Field(None, max_length=32,
                                   description="Account name from fb-watcher's config; default: the first")


class FetchPostResponse(BaseModel):
    schema_: Literal[1] = Field(SCHEMA, alias="schema")
    url: str
    post_id: str
    group_id: Optional[str]
    group_key: str
    group_name: Optional[str]
    author: Optional[str]
    text: str
    published_at: Optional[str]
    published_label: Optional[str]
    images: list[str]
    image_paths: list[str]
    fetched_at: str

    model_config = {"populate_by_name": True}


class CheckGroupRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(max_length=2048, description="Facebook group link or numeric ID")
    account: Optional[str] = Field(None, max_length=32)


class CheckGroupResponse(BaseModel):
    name: Optional[str]
    visibility: Literal["public", "private", "unavailable"]
    note: str
    group_id: Optional[str]


class ErrorResponse(BaseModel):
    schema_: Literal[1] = Field(SCHEMA, alias="schema")
    error: str
    message: str

    model_config = {"populate_by_name": True}


class HealthResponse(BaseModel):
    status: Literal["ok"]


class AccountStatus(BaseModel):
    name: str
    state: Literal["ok", "paused", "unknown"]
    reason: str
    last_round: Optional[str]


class GroupStatus(BaseModel):
    account: str
    group_id: str
    name: str
    last_ok: Optional[str]
    error: str
    failed_rounds: int
    failing_since: str
    incomplete_rounds: int


class DeliveryStatus(BaseModel):
    pending: int
    failing: int


class StatusResponse(BaseModel):
    accounts: list[AccountStatus]
    groups: list[GroupStatus]
    deliveries: DeliveryStatus


class ApiError(Exception):
    def __init__(self, kind, message, retry_after=None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


# --- browser work, in a worker process ------------------------------------------

def call_browser(kind, request, timeout):
    """Run one fetch/check in `watcher.py api-worker`; return its result or raise ApiError."""
    try:
        reply = watcher.call_worker("api-worker", {"kind": kind, **request}, timeout, quiet=True)
    except watcher.WorkerTimeout:
        raise ApiError("timeout", f"{kind} took longer than {timeout}s and was stopped") from None
    except watcher.ScanWorkerError as e:
        raise ApiError("error", str(e)) from None
    if reply.get("ok"):
        return reply["result"]
    kind = reply.get("error", "error")
    retry = {"profile_busy": RETRY_AFTER_SECONDS, "login_required": LOGIN_RETRY_AFTER_SECONDS}.get(kind)
    raise ApiError(kind, reply.get("message", ""), retry)


def cmd_api_worker():
    """Internal: one fetch or check described on stdin; prints one JSON line."""
    import fetch_post as fp

    request = json.loads(sys.stdin.read())
    try:
        if request["kind"] == "fetch":
            result = fp.fetch_post(request["url"], request.get("account"), lock_wait=LOCK_WAIT_SECONDS,
                                   public_only=True)
        else:
            result = fp.check_group(request["url"], request.get("account"), lock_wait=LOCK_WAIT_SECONDS)
        reply = {"ok": True, "result": result}
    except fp.FetchError as e:
        reply = {"ok": False, "error": e.kind, "message": str(e)[:300]}
    except Exception as e:  # unexpected: type only, no page content
        reply = {"ok": False, "error": "error", "message": type(e).__name__}
    print(json.dumps(reply, ensure_ascii=False), flush=True)


# --- limits -----------------------------------------------------------------------

class RateLimiter:
    """At most `per_hour` browser calls per account in any rolling hour."""

    def __init__(self, per_hour, clock=time.monotonic):
        self.per_hour, self.clock, self.calls, self.lock = per_hour, clock, {}, threading.Lock()

    def take(self, account):
        now = self.clock()
        with self.lock:
            calls = self.calls.setdefault(account, deque())
            while calls and now - calls[0] >= 3600:
                calls.popleft()
            if len(calls) >= self.per_hour:
                raise ApiError("rate_limited", f"more than {self.per_hour} Facebook calls this hour for this account",
                               int(3600 - (now - calls[0])) + 1)
            calls.append(now)


class Cache:
    def __init__(self, seconds, clock=time.monotonic):
        self.seconds, self.clock, self.items, self.lock = seconds, clock, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            hit = self.items.get(key)
            if hit and self.clock() - hit[0] < self.seconds:
                return hit[1]
            self.items.pop(key, None)
            return None

    def drop_group(self, group_id):
        with self.lock:
            for key in [k for k, (_, v) in self.items.items() if isinstance(v, dict) and v.get("group_id") == group_id]:
                del self.items[key]

    def put(self, key, value):
        with self.lock:
            if len(self.items) > 1000:
                self.items.clear()
            self.items[key] = (self.clock(), value)


# --- status (read-only from seen.db) --------------------------------------------

def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def read_status(db_path, account_names):
    """Health from what the run loop records. Names, IDs, error types and counts only."""
    import sqlite3

    empty = {"accounts": [{"name": n, "state": "unknown", "reason": "", "last_round": None} for n in account_names],
             "groups": [], "deliveries": {"pending": 0, "failing": 0}}
    if not Path(db_path).exists():
        return empty
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "account_status" not in tables:
            return empty
        known = {r[0]: r for r in db.execute("SELECT account, state, reason, last_round FROM account_status")}
        accounts = [{"name": n, "state": known[n][1] if n in known else "unknown",
                     "reason": known[n][2] if n in known else "", "last_round": _iso(known[n][3]) if n in known else None}
                    for n in account_names]
        incomplete = dict(db.execute("SELECT group_id, incomplete FROM group_state")) if "group_state" in tables else {}
        groups = [{"account": a, "group_id": g, "name": name, "last_ok": _iso(ok), "error": err,
                   "failed_rounds": rounds, "failing_since": since, "incomplete_rounds": incomplete.get(g, 0)}
                  for a, g, name, ok, err, rounds, since in db.execute(
                      "SELECT account, group_id, name, last_ok, error, failed_rounds, failing_since FROM group_status "
                      "ORDER BY account, group_id")
                  if a in account_names]
        pending, failing = db.execute("SELECT COUNT(*), COALESCE(SUM(attempts >= 3), 0) FROM outbox").fetchone() \
            if "outbox" in tables else (0, 0)
        return {"accounts": accounts, "groups": groups, "deliveries": {"pending": pending, "failing": failing}}
    finally:
        db.close()


# --- app --------------------------------------------------------------------------

def fetch_cache_key(url):
    """One key per post however it's linked (host, /permalink/ vs /posts/, tracking query)."""
    import fetch_post as fp

    url = url.strip()
    if not fp._supported_url(url):
        return url
    link = fp.parse_post_url(fp._desktop_url(url).split("?", 1)[0].split("#", 1)[0])
    if link:
        return f"https://www.facebook.com/groups/{link.group_key}/posts/{link.post_id}/"
    return fp._desktop_url(url).split("?", 1)[0].split("#", 1)[0]


def read_paused(db_path):
    """Accounts the watcher has paused (logged out / account check), from seen.db."""
    import sqlite3

    if not Path(db_path).exists():
        return {}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'account_status'").fetchone():
            return {}
        return dict(db.execute("SELECT account, reason FROM account_status WHERE state = 'paused'"))
    finally:
        db.close()


def create_app(*, key, account_names, browser=call_browser, db_path=None, calls_per_hour=DEFAULT_CALLS_PER_HOUR,
               cache_seconds=DEFAULT_CACHE_SECONDS, docs=False, clock=time.monotonic):
    if not key:
        raise ValueError("an API key is required")
    expected = key.encode()
    db_path = db_path or watcher.DB_PATH
    default_account = account_names[0] if account_names else None
    limiter, cache = RateLimiter(calls_per_hour, clock), Cache(cache_seconds, clock)
    slots = {name: threading.Lock() for name in account_names}  # one browser job per account
    cooldown = {}  # account -> clock time until which it rests after a login problem
    bearer = HTTPBearer(auto_error=False, description="The key from FB_WATCHER_API_KEY_FILE / api.key_file")
    app = FastAPI(title="fb-watcher API", version=str(SCHEMA), docs_url="/docs" if docs else None,
                  redoc_url="/redoc" if docs else None, openapi_url="/openapi.json")

    def error_response(kind, message, retry_after=None):
        headers = {"Retry-After": str(int(retry_after))} if retry_after else {}
        if kind == "unauthorized":
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(status_code=STATUS_FOR.get(kind, 502), headers=headers,
                            content={"schema": SCHEMA, "error": kind, "message": message})

    def key_ok(header):
        scheme, _, token = (header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode(), expected)

    @app.exception_handler(ApiError)
    def _api_error(request, exc):
        return error_response(exc.kind, str(exc), exc.retry_after)

    @app.exception_handler(RequestValidationError)
    def _invalid(request, exc):
        return error_response("bad_request", "invalid request body")

    @app.exception_handler(StarletteHTTPException)
    def _http_error(request, exc):
        kind = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(exc.status_code, "bad_request")
        response = error_response(kind, kind.replace("_", " "))
        if exc.status_code == 405 and exc.headers and "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
        return response

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        start = time.monotonic()
        path = request.url.path
        # The key is checked before any body is read, so anonymous callers can't make the
        # server buffer or parse anything. /healthz and the contract are open.
        if path not in ("/healthz", "/openapi.json", "/docs", "/redoc") and not key_ok(request.headers.get("authorization")):
            response = error_response("unauthorized", "missing or wrong API key")
        elif request.method == "POST" and not (request.headers.get("content-length") or "").isdigit():
            # Bodies must say their size up front, so an oversized one is never read.
            response = error_response("length_required", "send a Content-Length header")
        elif int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            response = error_response("too_large", f"request body over {MAX_BODY_BYTES} bytes")
        else:
            try:
                response = await call_next(request)
            except ApiError as e:
                response = error_response(e.kind, str(e), e.retry_after)
            except Exception as e:  # never let a traceback (which may hold post text) reach the server log
                response = error_response("error", type(e).__name__)
        # Method, path and status only: never bodies, query strings, keys or cookies.
        log.info("%s %s %s %.0fms", request.method, path, response.status_code, (time.monotonic() - start) * 1000)
        return response

    def authorized(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
        # Documents bearer auth in /openapi.json; the middleware has already checked the key.
        return credentials

    def resolve_account(name):
        if name is None:
            if default_account is None:
                raise ApiError("bad_account", "no accounts configured")
            return default_account
        if name not in account_names:
            raise ApiError("bad_account", "unknown account")
        return name

    def run_browser(account, kind, request, timeout, model, cache_key, group_key=None):
        """Admission: cache, pause/cooldown, one job per account, hourly budget, then the
        worker. Identical concurrent requests wait for the first and get its cached answer."""
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
        paused = read_paused(db_path).get(account)
        if paused:
            raise ApiError("login_required", f"the watcher paused this account ({paused.split(' on ')[0]})",
                           LOGIN_RETRY_AFTER_SECONDS)
        if cooldown.get(account, 0) > clock():
            raise ApiError("login_required", "this account hit a login problem recently",
                           int(cooldown[account] - clock()) + 1)
        if not slots[account].acquire(timeout=LOCK_WAIT_SECONDS):
            raise ApiError("profile_busy", "another request for this account is running", RETRY_AFTER_SECONDS)
        try:
            hit = cache.get(cache_key)
            if hit is not None:
                return hit
            limiter.take(account)
            try:
                result = browser(kind, request, timeout)
            except ApiError as e:
                if e.kind == "login_required":
                    cooldown[account] = clock() + LOGIN_RETRY_AFTER_SECONDS
                    e.retry_after = e.retry_after or LOGIN_RETRY_AFTER_SECONDS
                raise
            try:
                result = model.model_validate(result).model_dump(by_alias=True)
            except ValidationError:
                raise ApiError("error", "the worker returned an unexpected result") from None
            cache.put(cache_key, result)
            return result
        finally:
            slots[account].release()

    @app.get("/healthz", response_model=HealthResponse)
    def healthz():
        return {"status": "ok"}

    @app.post("/fetch-post", response_model=FetchPostResponse, response_model_by_alias=True,
              dependencies=[Depends(authorized)])
    def fetch_post_endpoint(body: FetchPostRequest):
        account = resolve_account(body.account)
        return run_browser(account, "fetch", {"url": body.url, "account": account}, FETCH_TIMEOUT,
                           FetchPostResponse, ("fetch", account, fetch_cache_key(body.url)))

    @app.post("/check-group", response_model=CheckGroupResponse, dependencies=[Depends(authorized)])
    def check_group_endpoint(body: CheckGroupRequest):
        account = resolve_account(body.account)
        result = run_browser(account, "check", {"url": body.url, "account": account}, CHECK_TIMEOUT,
                             CheckGroupResponse, ("check", account, body.url.strip()))
        if result["visibility"] != "public" and result.get("group_id"):
            cache.drop_group(result["group_id"])  # posts cached while it looked public go too
        return result

    @app.get("/status", response_model=StatusResponse, dependencies=[Depends(authorized)])
    def status():
        return read_status(db_path, account_names)

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        spec = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path in spec["paths"].values():
            for op in path.values():
                responses = op.setdefault("responses", {})
                responses.pop("422", None)
                if op.get("security"):
                    for code in ("400", "401", "403", "404", "411", "413", "429", "502", "503", "504"):
                        responses[code] = {"description": "Error", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        for name in ("HTTPValidationError", "ValidationError"):
            schemas.pop(name, None)
        schemas["ErrorResponse"] = ErrorResponse.model_json_schema(by_alias=True)
        app.openapi_schema = spec
        return spec

    app.openapi = openapi
    return app


def load_key(cfg):
    path = os.environ.get("FB_WATCHER_API_KEY_FILE") or (cfg.get("api") or {}).get("key_file")
    if not path:
        raise SystemExit("no API key: set FB_WATCHER_API_KEY_FILE or api.key_file in config.json")
    try:
        key = Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"can't read the API key file ({type(e).__name__})") from None
    if len(key) < 16:
        raise SystemExit("the API key file must hold a key of at least 16 characters")
    return key


def cmd_api(args):
    """`watcher.py api [--host 127.0.0.1] [--port 8091] [--docs]`."""
    import uvicorn

    host, port, docs = "127.0.0.1", 8091, "--docs" in args
    args = [a for a in args if a != "--docs"]
    try:
        while args:
            flag, value = args.pop(0), args.pop(0)
            if flag == "--host":
                host = value
            elif flag == "--port":
                port = int(value)
            else:
                raise ValueError(flag)
    except (IndexError, ValueError):
        raise SystemExit("usage: watcher.py api [--host 127.0.0.1] [--port 8091] [--docs]") from None
    cfg = watcher.load_config()
    api_cfg = cfg.get("api") or {}
    app = create_app(key=load_key(cfg), account_names=[a["name"] for a in watcher.load_accounts(cfg)],
                     calls_per_hour=int(api_cfg.get("calls_per_hour", DEFAULT_CALLS_PER_HOUR)),
                     cache_seconds=int(api_cfg.get("cache_seconds", DEFAULT_CACHE_SECONDS)), docs=docs)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    uvicorn.run(app, host=host, port=port, access_log=False, server_header=False)
