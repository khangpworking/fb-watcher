# fb-watcher

Polls private Facebook groups you're a member of, finds new posts that match your keywords, and sends them to Telegram.

Stack: Python + Playwright. Scraping uses [fbn](https://github.com/viseshrp/fbn)'s mobile reader, pinned to one commit in `requirements.txt`; it reads `m.facebook.com` as a Pixel 7 and only returns posts whose link it has verified. The logged-in browser profile is kept in `./profile`, `seen.db` (SQLite) records posts already handled, and alerts go to Telegram and braveduck.

## Setup

1. **Telegram bot**
   - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` from the response.
2. **Config**: edit `config.json`.
   - `groups`: the group ID is the number (or slug) in `facebook.com/groups/<id>`.
   - `keywords`: case and Vietnamese accents are ignored. End a keyword with `*` to match by prefix, e.g. `automat*`.
   - `telegram_token` / `telegram_chat_id`: put them here, or set the env vars `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
3. **Log in once.** Use one of these:
   - Open a visible browser through WSLg, log in, then press Enter in the terminal:
     ```bash
     .venv/bin/python watcher.py login
     ```
   - Or export your facebook.com cookies from a browser that is already logged in. The file can be a cookie-extension JSON, a Netscape `cookies.txt`, or a Playwright storage state. Keep the file outside this folder and `chmod 600` it. Import it with:
     ```bash
     .venv/bin/python watcher.py import-cookies ~/.config/fb-watcher/facebook-auth.json
     ```
     This also checks that the account can open the first group in `config.json`.
4. **Test the alert**:
   ```bash
   .venv/bin/python watcher.py test-alert
   ```
5. **Run**:
   ```bash
   .venv/bin/python watcher.py run
   ```

The first pass over each group records the existing posts without sending alerts. After that, you get an alert only for new posts that match a keyword.

## Several accounts and proxies

Each account has its own browser profile, its own optional proxy, and its own list of groups. A group can be listed under only one account.

```json
"accounts": [
  { "name": "default", "proxy": "", "groups": [{ "id": "3989249071208661", "name": "Tuyển Dụng AI Engineer Việt Nam" }] },
  { "name": "second", "proxy": "http://user:pass@203.0.113.10:8080", "groups": [{ "id": "1329241984892387", "name": "Việc làm AI Engineer -Việt Nam" }] }
]
```

- **Names:** lowercase letters, digits, `_` and `-`. The account named `default` uses `./profile`, so a config with only the old top-level `groups` list still works as one `default` account. Other accounts get `./profiles/<name>`.
- **Proxy:** `http://`, `https://` or `socks5://`, written as `scheme://[user:pass@]host:port`. URL-encode special characters in the username or password, e.g. `@` becomes `%40`. Chromium can't use a SOCKS5 proxy that needs a username and password, so use an `http://` proxy for those. The proxy is used for the login window, cookie import and every scan, so Facebook always sees the account at the same address. Give each account its own proxy, and keep it the same over time.
- **Commands per account:**
  ```bash
  .venv/bin/python watcher.py check-proxy second
  .venv/bin/python watcher.py login second
  .venv/bin/python watcher.py import-cookies ~/.config/fb-watcher/second.json second
  ```
  `check-proxy` prints the public IP the account's browser uses. Run it before logging in, to confirm the proxy works.
- **Logged out:** if an account is logged out or needs an account check, only its groups pause. You get a Telegram warning naming the account, and the board shows `<error> on account <name>`. The service keeps running the other accounts, and stops (exit 3) only when every account is down. After logging the account in again, restart the service.

## braveduck job board

When `braveduck.enabled` is true, each matching post is also added with `braveduck add`:

| Job field | Taken from |
|---|---|
| title | First line of the post, cut to 120 characters |
| company | Group name |
| source | `facebook` |
| url | Post link (braveduck uses this to skip duplicates) |
| jd | Post text, plus author, group and matched keywords |

- After each round, fb-watcher reports scan health with `braveduck crawl-report facebook --ok` (or `--fail "<error type>: <message>"`), so the board shows when Facebook scanning is broken or logged out. If any one group fails 3 rounds in a row, it reports `--fail "<error type> on <group> since HH:MM"` even while other groups work. Only fbn's fixed error text is sent, never post content. On a braveduck build without `crawl-report`, the call fails quietly.
- `db` is empty by default, which uses `~/.braveduck/braveduck.db`. Set it to point at a different board.

## Notes

- This machine has no root access, so Chromium's missing system libraries (`libnss3`, `libnspr4`, `libasound2t64`) were unpacked into `libs/root` with `apt-get download` and `dpkg-deb -x`. `watcher.py` adds that folder to `LD_LIBRARY_PATH` itself.
- Scraping breaks Facebook's ToS. Use a secondary account that has joined the groups, and keep `poll_seconds` at 60 or higher.
- Each scan scrolls back until it reaches the newest post from the previous scan (the `boundary` table), up to `scrolls` scrolls and `sample_count` posts. The boundary only moves once a scan actually reaches it, so posts in a gap stay pending and get read in a later round. If 3 scans in a row fall short (for example, the boundary post was deleted), it moves anyway, and the board shows `incomplete scans on <group>`. Raise `sample_count` (maximum 50) for busy groups.
- Matching posts go through an outbox in `seen.db`. A post is marked as handled in the same step as its braveduck and Telegram deliveries are queued. Every round retries whatever is still queued, including after a restart, and a delivery that has failed 3 times shows on the board as `N of M deliveries failing`.
- Each group scan runs as a separate process (`watcher.py scan-worker`). If a scan runs past 4 minutes, that process and all of its browser processes are killed, and only that group is skipped for the round.
- If you're logged out, Facebook asks for an account action, or the browser can't start, the watcher sends a Telegram warning and exits. Timeouts, page-layout changes and access errors only skip that group for the current round.
- `fbn_patch.py` patches fbn where it misses or mishandles posts (each patch is described at the top of the file):
  - a post fbn can't fully read is skipped, instead of failing the whole group;
  - posts labelled "AI content", and posts the account has already liked, are read normally;
  - Facebook's post "…" menu broke on 2026-11-04, so links come from tapping the post's timestamp instead (fbn still verifies them);
  - if one post's link can't be read, the scan ends early and keeps the posts read so far;
  - a post whose own page fbn can't render is kept only if the page's address still names the same post.
  - posts already handled are remembered (`post_cache` in `seen.db`), so scans don't tap their timestamp or open their page again. A round costs one feed load plus a visit per new post. Before this, every round revisited every visible post, roughly 300 post pages an hour, and Facebook started returning empty post pages.

  If a patch no longer fits a new fbn version, the watcher stops at startup with a clear error.
- Group IDs must be the numeric ID from the group's URL. Group names (slugs) are rejected, because post links are checked against the numeric ID.
- Posts larger than 100 KB are cut to 100 KB in braveduck, with a note pointing to the post link.
- When Facebook changes its layout, update fbn: change the commit in `requirements.txt`, then run `.venv/bin/pip install -r requirements.txt`. If fbn fails, it saves screenshots and says where in its error message.

## Run as a service

`deploy/fb-watcher.service` is a systemd user service. It runs `watcher.py run`, restarts 60 seconds after a crash, and stays stopped after exit code 3 (logged out or account check required, which needs you).

```bash
ln -sf ~/fb-watcher/deploy/fb-watcher.service ~/.config/systemd/user/fb-watcher.service
systemctl --user daemon-reload
systemctl --user enable --now fb-watcher
```

To watch its output:

```bash
journalctl --user -u fb-watcher -f
```

After logging in again, start it:

```bash
systemctl --user restart fb-watcher
```

On WSL, user services only run while a WSL session is open. To keep it running without one, run `loginctl enable-linger $USER`; this may need admin rights.

## As braveduck's scanner (shared server)

On a braveduck shared server, people follow Facebook groups by pasting a
group's link on their board. fb-watcher reads those groups for them, with one
logged-in account:

```bash
python watcher.py serve-braveduck
```

Each round it asks braveduck which groups are followed, opens each new group's
page once a day to read its name and whether it's a **public** group, scans
public groups, and hands their new posts to braveduck, which matches them to
each person's own search terms. A group counts as public only when it's the group's own
page (its own ID, not a login, checkpoint or "unavailable" page) and its header
reads "Public group · N members" (or "Nhóm Công khai · N thành viên"); private
groups and unclear ones are reported and never scanned for others, since their
posts are for members. The same check guards the HTTP API.

Config: `braveduck_service` in `config.json`, or the environment (the
Dockerfile sets these, and needs no `config.json`):

```json
"braveduck_service": { "url": "http://braveduck:8090", "key_file": "/run/secrets/scanner_secret" }
```
`BRAVEDUCK_SCANNER_URL` / `BRAVEDUCK_SCANNER_KEY_FILE`.

Check what it sees on one group before relying on it:

```bash
python watcher.py check-group https://www.facebook.com/groups/<id>/
```

braveduck's `deploy/hosted` runs this from the `Dockerfile` here (compose
profile `facebook`); `FB_WATCHER_DATA` puts the login profile, `seen.db` and
`config.json` in a volume.

## Fetch one post

Read a single group post by its link, on demand. Other tools can call it from the command line or import it.

```bash
.venv/bin/python watcher.py fetch-post URL [ACCOUNT] [--json] [--assets-dir DIR]
```

```python
from fetch_post import fetch_post, FetchError
post = fetch_post("https://www.facebook.com/groups/<group>/posts/<post>/", account="default", assets_dir="/tmp/imgs")
```

- **URL:** `www.`, `m.` or `web.facebook.com` group links (`/groups/<g>/posts/<id>/`, `/groups/<g>/permalink/<id>/`), or share links (`/share/p/<code>/`).
- **ACCOUNT:** an account name from the config; defaults to the first account. Uses that account's profile and proxy.
- **`--public-only`:** return the post only if its group's page says "Public group" (or "Nhóm Công khai"). Each group's visibility is checked once a day and remembered in `seen.db` (`group_visibility`).
- **How it reads:** it opens the post on the **desktop** site. Since 2026-11-05, Facebook answers direct loads of mobile post pages with an empty page, while the desktop post page renders in full, including posts in public groups the account hasn't joined. It reads only that one post, never a feed.
- **Sharing the profile with the watcher:** fetch-post takes the profile's lock for its whole read, about 10–20 seconds. If a scan is running it waits, for up to 4 minutes. A scan that finds fetch-post holding the lock also waits, for up to 90 seconds.

`--json` prints one object. Schema version 1:

| Field | Meaning |
|---|---|
| `schema` | `1`. A new number means fields changed incompatibly. |
| `url` | Canonical link, `https://www.facebook.com/groups/<group>/posts/<post_id>/` |
| `post_id`, `group_key` | IDs from the link. `group_id` is the same as `group_key` when it's numeric, otherwise `null`. |
| `group_name`, `author` | As shown on the post (`null` if not found) |
| `text` | Full post text, with "See more" expanded |
| `published_at` | ISO 8601 with offset, from the date tooltip, read in `timezone` from the config (default `Asia/Ho_Chi_Minh`); `null` if not read. `published_label` is the tooltip text. |
| `images` | Image URLs in the post (not comments). These Facebook CDN links expire after a few days. |
| `image_paths` | With `--assets-dir`, the downloaded files (`<post_id>-<n>.<ext>`); otherwise `[]` |
| `fetched_at` | When it was read (UTC) |

If it fails, `--json` prints `{"schema": 1, "error": "<kind>", "message": "..."}`.

| Exit code | `error` | Meaning |
|---|---|---|
| 0 | | OK |
| 1 | `error` | Anything else: a browser or network error, or a timeout |
| 2 | `bad_url`, `bad_account` | Not a supported link, unknown account, or bad arguments |
| 3 | `login_required` | The account isn't logged in, or Facebook wants an account check |
| 4 | `not_visible` | Post deleted, private, or in a group the account hasn't joined |
| 5 | `profile_busy` | The profile stayed busy for 4 minutes |
| 6 | `not_public` | With `--public-only`: the post's group isn't a confirmed public group, so nothing of the post is returned |

## HTTP API

A small HTTP API, so other services (for example braveduck's Go server, in another container) can use fetch-post and check-group without running Python. It doesn't change the watcher, which keeps running as before.

```bash
.venv/bin/python watcher.py api [--host 127.0.0.1] [--port 8091] [--docs]
```

- **Key:** required. Put a random key of at least 16 characters in a file (`head -c 32 /dev/urandom | base64 > ~/.config/fb-watcher/api.key; chmod 600 …`), then point `FB_WATCHER_API_KEY_FILE`, or `api.key_file` in the config, at it. The API won't start without one. Every endpoint except `/healthz` and `/openapi.json` needs `Authorization: Bearer <key>`. The key is checked before the request body is read.
- **Requests:** POST bodies must send `Content-Length` (Go's HTTP client does) and be at most 16 KB. Unknown fields are refused.
- **Address:** the default `127.0.0.1` is reachable only from this machine. In a container, use `--host 0.0.0.0` so other containers on the same network can reach it; the key still applies.
- **Docs:** `/docs` and `/redoc` are off unless you pass `--docs`. `/openapi.json` is always open and describes the contract, including bearer auth and the error responses; it holds no secrets.
- **Config (optional):** `"api": {"key_file": "…", "calls_per_hour": 30, "cache_seconds": 600}`.
- **Protecting the account:**
  - each account runs one browser job at a time; a second request waits up to 15 seconds, then gets `503 profile_busy`. Identical requests that arrive together share the first one's answer;
  - each account allows at most `calls_per_hour` Facebook calls in any rolling hour, then answers `429` with `Retry-After`;
  - the same post (however it's linked: `m.`/`www.`, `/permalink/` or `/posts/`, tracking parameters) within `cache_seconds` gets the cached answer and doesn't load Facebook again;
  - an account the watcher has paused (logged out, account check) is refused with `503 login_required`. After the API itself hits a login problem, that account rests for 30 minutes;
  - every call runs in its own worker process and waits at most 15 seconds for the profile, which the watcher's scans share. It's killed after 120 seconds (fetch) or 90 seconds (check), together with every browser process it started.
- **Privacy:** `/fetch-post` only returns posts from groups confirmed public, and callers can't turn that off. A group counts as public only when:
  - its own page loads (not a login, checkpoint or "unavailable" page) and names the group's own ID;
  - its header reads "Public group · N members" (or "Nhóm Công khai · N thành viên").

  Anything unclear counts as not public. A public or private answer is reused for an hour; unclear answers and login problems aren't remembered. When `/check-group` finds a group isn't public, cached posts from it are dropped.
- **Logs:** method, path, status and duration only. Errors are caught before the server logs them, and workers' browser output is discarded.

| Endpoint | Request | Response |
|---|---|---|
| `GET /healthz` | (no key) | `{"status": "ok"}` |
| `POST /fetch-post` | `{"url": "…", "account": null}` | The schema-1 object from "Fetch one post" |
| `POST /check-group` | `{"url": "<group link or ID>", "account": null}` | `{"name", "visibility": "public" \| "private" \| "unavailable", "note", "group_id"}` |
| `GET /status` | | `{"accounts": [{"name", "state": "ok" \| "paused" \| "unknown", "reason", "last_round"}], "groups": [{"account", "group_id", "name", "last_ok", "error", "failed_rounds", "failing_since", "incomplete_rounds"}], "deliveries": {"pending", "failing"}}` |

`/status` is read from what `watcher.py run` saves each round (error types only, never post text, cookies, proxies or tokens). It shows `unknown` until the watcher has run a round on this version.

Errors always come back as `{"schema": 1, "error": "<kind>", "message": "…"}`:

| HTTP | `error` |
|---|---|
| 400 | `bad_url`, `bad_account`, `bad_request` |
| 401 | `unauthorized` (with `WWW-Authenticate: Bearer`) |
| 403 | `not_public` |
| 404 | `not_visible`, or `not_found` for an unknown path |
| 405 | `method_not_allowed` (with `Allow`) |
| 411 | `length_required` |
| 413 | `too_large` |
| 429 | `rate_limited` (with `Retry-After`) |
| 502 | `error` |
| 503 | `login_required`, `profile_busy` (both with `Retry-After`) |
| 504 | `timeout` |

Times are ISO 8601: `published_at` and `fetched_at` carry an offset; `/status` times are UTC (`…Z`). IDs are strings. Fields shown as `null` in the schema are always present.

```bash
curl -s -X POST http://127.0.0.1:8091/fetch-post \
  -H "Authorization: Bearer $(cat ~/.config/fb-watcher/api.key)" -H "Content-Type: application/json" \
  -d '{"url": "https://www.facebook.com/groups/<group>/posts/<post>/"}'
```

**In Docker (optional):** the image already includes the API. To run it, start the container with the command `api --host 0.0.0.0 --port 8091` and mount the key file. braveduck's `deploy/hosted` files aren't changed here.

## Tests

```bash
.venv/bin/python tests/test_run_loop.py     # run loop, outbox, boundary, scan worker; no Facebook
.venv/bin/python tests/test_fetch_post.py   # fetch-post arguments, URLs, exit codes; no Facebook
.venv/bin/python tests/test_api.py          # HTTP API with fakes; no Facebook
```
