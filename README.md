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

## braveduck job board

When `braveduck.enabled` is true, each matching post is also added with `braveduck add`:

| Job field | Taken from |
|---|---|
| title | First line of the post, cut to 120 characters |
| company | Group name |
| source | `facebook` |
| url | Post link (braveduck uses this to skip duplicates) |
| jd | Post text, plus author, group and matched keywords |

- `db` is empty by default, which uses `~/.braveduck/braveduck.db`. Set it to point at a different board.

## Notes

- This machine has no root access, so Chromium's missing system libraries (`libnss3`, `libnspr4`, `libasound2t64`) were unpacked into `libs/root` with `apt-get download` and `dpkg-deb -x`. `watcher.py` adds that folder to `LD_LIBRARY_PATH` itself.
- Scraping breaks Facebook's ToS. Use a secondary account that has joined the groups, and keep `poll_seconds` at 60 or higher.
- Each scan scrolls back until it reaches the newest post from the previous scan (the `boundary` table), up to `scrolls` scrolls and `sample_count` posts. If more than `sample_count` posts arrive between two scans, the oldest ones are missed; raise `sample_count` (maximum 50) for busy groups.
- If you're logged out, Facebook asks for an account action, or the browser can't start, the watcher sends a Telegram warning and exits. Timeouts, page-layout changes and access errors only skip that group for the current round.
- `fbn_patch.py` makes fbn skip a post it can't fully read, such as a shared post or one with a "Follow" button next to the author, instead of failing the whole group. If the patch no longer fits a new fbn version, the watcher stops at startup with a clear error.
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
