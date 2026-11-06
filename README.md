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
each person's own search terms. A group counts as public only when its page
says "Public group" (or "Nhóm Công khai"); private groups and unclear ones are
reported and never scanned for others, since their posts are for members.

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
