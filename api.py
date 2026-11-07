"""fb-watcher's HTTP API: fetch-post and check-group for other services (braveduck's Go
server, in another container), plus health and status. Run: `python watcher.py api`.

Every browser call runs in its own worker process (`watcher.py api-worker`), takes the
account's profile lock (waiting at most LOCK_WAIT_SECONDS), and is killed with its browser
at its time limit. Calls are rate-limited per account and fetch results are cached
briefly, so callers can't drive Facebook page loads. See README "HTTP API".
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

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import watcher

SCHEMA = 1
LOCK_WAIT_SECONDS = 15
FETCH_TIMEOUT = 120
CHECK_TIMEOUT = 90
RETRY_AFTER_SECONDS = 30
DEFAULT_CALLS_PER_HOUR = 30
DEFAULT_CACHE_SECONDS = 600

log = logging.getLogger("fb_watcher.api")

# error kind -> HTTP status. The body is always ErrorResponse.
STATUS_FOR = {
    "bad_url": 400, "bad_account": 400, "bad_request": 400,
    "unauthorized": 401,
    "not_public": 403,
    "not_visible": 404,
    "rate_limited": 429,
    "error": 502,
    "login_required": 503, "profile_busy": 503,
    "timeout": 504,
}


# --- contract -----------------------------------------------------------------

class FetchPostRequest(BaseModel):
    url: str = Field(description="Facebook group post or share link")
    account: Optional[str] = Field(None, description="Account name from fb-watcher's config; default: the first")
    public_only: bool = Field(True, description="Refuse posts whose group isn't a confirmed public group")


class FetchPostResponse(BaseModel):
    schema_: int = Field(SCHEMA, alias="schema")
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
    url: str = Field(description="Facebook group link or ID")
    account: Optional[str] = None


class CheckGroupResponse(BaseModel):
    name: Optional[str]
    visibility: Literal["public", "private", "unavailable"]
    note: str
    group_id: Optional[str]


class ErrorResponse(BaseModel):
    schema_: int = Field(SCHEMA, alias="schema")
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
        reply = watcher.call_worker("api-worker", {"kind": kind, **request}, timeout)
    except watcher.WorkerTimeout:
        raise ApiError("timeout", f"{kind} took longer than {timeout}s and was stopped") from None
    except watcher.ScanWorkerError as e:
        raise ApiError("error", str(e)) from None
    if reply.get("ok"):
        return reply["result"]
    raise ApiError(reply.get("error", "error"), reply.get("message", ""),
                   RETRY_AFTER_SECONDS if reply.get("error") == "profile_busy" else None)


def cmd_api_worker():
    """Internal: one fetch or check described on stdin; prints one JSON line."""
    import fetch_post as fp

    request = json.loads(sys.stdin.read())
    try:
        if request["kind"] == "fetch":
            result = fp.fetch_post(request["url"], request.get("account"), lock_wait=LOCK_WAIT_SECONDS,
                                   public_only=request.get("public_only", True))
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

def create_app(*, key, account_names, browser=call_browser, db_path=None, calls_per_hour=DEFAULT_CALLS_PER_HOUR,
               cache_seconds=DEFAULT_CACHE_SECONDS, docs=False, clock=time.monotonic):
    if not key:
        raise ValueError("an API key is required")
    expected = key.encode()
    default_account = account_names[0] if account_names else None
    limiter, cache = RateLimiter(calls_per_hour, clock), Cache(cache_seconds, clock)
    app = FastAPI(title="fb-watcher API", version=str(SCHEMA), docs_url="/docs" if docs else None,
                  redoc_url="/redoc" if docs else None,
                  responses={code: {"model": ErrorResponse} for code in sorted(set(STATUS_FOR.values()))})

    def error_response(kind, message, retry_after=None):
        headers = {"Retry-After": str(retry_after)} if retry_after else {}
        if kind == "unauthorized":
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(status_code=STATUS_FOR.get(kind, 502), headers=headers,
                            content={"schema": SCHEMA, "error": kind, "message": message})

    @app.exception_handler(ApiError)
    def _api_error(request, exc):
        return error_response(exc.kind, str(exc), exc.retry_after)

    @app.exception_handler(Exception)
    def _unexpected(request, exc):
        return error_response("error", type(exc).__name__)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    def _invalid(request, exc):
        return error_response("bad_request", "invalid request body")

    @app.middleware("http")
    async def _log(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        # Method, path and status only: never bodies, query strings, keys or cookies.
        log.info("%s %s %s %.0fms", request.method, request.url.path, response.status_code,
                 (time.monotonic() - start) * 1000)
        return response

    def authorized(authorization: Optional[str] = Header(None)):
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), expected):
            raise ApiError("unauthorized", "missing or wrong API key")

    def resolve_account(name):
        if name is None:
            if default_account is None:
                raise ApiError("bad_account", "no accounts configured")
            return default_account
        if name not in account_names:
            raise ApiError("bad_account", "unknown account")
        return name

    @app.get("/healthz", response_model=HealthResponse)
    def healthz():
        return {"status": "ok"}

    @app.post("/fetch-post", response_model=FetchPostResponse, response_model_by_alias=True,
              dependencies=[Depends(authorized)])
    def fetch_post_endpoint(body: FetchPostRequest):
        account = resolve_account(body.account)
        cache_key = ("fetch", account, body.url.strip(), body.public_only)
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
        limiter.take(account)
        result = browser("fetch", {"url": body.url, "account": account, "public_only": body.public_only},
                         FETCH_TIMEOUT)
        cache.put(cache_key, result)
        return result

    @app.post("/check-group", response_model=CheckGroupResponse, dependencies=[Depends(authorized)])
    def check_group_endpoint(body: CheckGroupRequest):
        account = resolve_account(body.account)
        cache_key = ("check", account, body.url.strip())
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
        limiter.take(account)
        result = browser("check", {"url": body.url, "account": account}, CHECK_TIMEOUT)
        cache.put(cache_key, result)
        return result

    @app.get("/status", response_model=StatusResponse, dependencies=[Depends(authorized)])
    def status():
        return read_status(db_path or watcher.DB_PATH, account_names)

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
