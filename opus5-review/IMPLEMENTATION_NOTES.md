# Reels-AutoPilot — Bulletproof Implementation Notes

Everything in the specification is implemented. No existing functionality was removed:
every previous module, function, config key, API endpoint, dashboard control and the
standalone scripts (`login_2fa.py`, `src/start.py`, `src/dashboard.py`, `src/purger.py`,
`src/remover.py`, `src/shorts.py`) still work exactly as before.

## New modules (`src/`)

| File | Purpose |
|---|---|
| `logger.py` | Rotating log file (5 MB × 3), console mirror, `read_recent_logs()` for the dashboard |
| `notifier.py` | Discord embeds with throttling/dedupe (login failure, recycling, swap, scrape failures, disk, watchdog) |
| `netcheck.py` | TCP connectivity probe, online/offline transition tracking |
| `diskspace.py` | Free-space checks, purge of posted media below the threshold, periodic usage logging |
| `statefile.py` | Atomic state persistence, pending-upload markers, cross-process upload lock |
| `accounts.py` | Posting-account CRUD, per-account runtime (client, timer, login status, health check), `AccountPool` |
| `distributor.py` | Round-robin assignment, A→B→C swapping, recycling, last-20 memory, verified posted writes |

Rewritten/extended: `config.py`, `db.py`, `auth.py`, `poster.py`, `reels.py`, `app.py`,
`helpers.py`, `web.py`, `static/dashboard.html`. New at repo root: `watchdog.py`, `systemd/`.

## Failure modes addressed

1. **Session/auth** — settings are dumped after every successful call, health check every 30 min via
   `account_info()`, login order `session.json → user/pass → SESSIONID`, 2FA accounts use the session
   cookie as primary and skip password login (`IS_2FA` / `is_2fa` persisted), Discord
   “⚠️ LOGIN FAILED — manual intervention needed” and a 5-minute retry cycle. The main loop never dies.
2. **`qe/expose` 404** — any post-upload configuration error is caught and the upload is verified with
   `user_medias(user_id, 1)`; a verified post is recorded as success.
3. **Content exhaustion** — fresh scrape first, then swap/recycle in batches of 10 (`FETCH_LIMIT`),
   oldest first from the account with the most posted reels, never repeating the last 20 codes,
   with the `[Poster] No new reels available. Recycling older content from @account...` log,
   a one-time Discord notice and a dashboard “Recycling” indicator.
4. **Scraper visibility** — per-account success/failure logs and DB status rows, dashboard card,
   Discord alert after 3 consecutive failures, 2–5 s delay between accounts.
5. **Duplicates** — file lock around uploads, unique index on `reels.code`, in-memory posted-code set,
   re-read inside the lock and read-back verification of the DB write.
6. **Crash recovery** — pending-upload marker written before every upload and reconciled against the
   Instagram API at startup, plus a DB self-check.
7. **Disk space** — pre-download check, purge of posted files under 500 MB free, hourly usage log,
   dashboard indicator.
8. **Network** — connectivity check before Instagram calls, 60 s retry while offline, transition logs,
   forced fresh login on reconnect.

## New features

- **Multi-account posting** — `posting_accounts` table, `reels.assigned_to / posted_by / swap_phase`,
  round-robin distribution, independent timer + `Client` + session file per account,
  A→B→C swapping (never back to an account in `posted_by`, full reset as last resort),
  per-account auth isolation, dashboard management card.
- **Self-healing session rotation** — challenged accounts are marked and skipped with 1h/4h/12h/24h backoff.
- **Dashboard** — live status pill, per-account stats and last post time, per-source scrape stats,
  log viewer, disk indicator, force-restart buttons and `GET /api/health`.
- **Watchdog** — `watchdog.py` systemd service running every 5 minutes.
- **Graceful shutdown** — SIGTERM/SIGINT finish the in-flight upload and save state.

## New API endpoints

`GET /api/health`, `GET /api/logs`, `GET /api/disk`, `GET /api/scrape_status`,
`GET|POST /api/accounts`, `PATCH|DELETE /api/accounts/<username>`, `POST /api/restart`.
All previous endpoints are unchanged.

## Migration

`db.migrate()` runs automatically on import: it adds the new columns with `ALTER TABLE`,
creates `posting_accounts` / `scrape_status`, enables WAL and adds the unique index on
`reels.code` (skipped automatically if duplicates already exist). **Back up `reels.db` first.**
Existing rows keep working; if no posting accounts exist, the legacy single-account flow from
Settings is used and is auto-imported into `posting_accounts` on first start.

## Deployment (Raspberry Pi)

```bash
cd /home/electro/reels-autopilot
cp reels.db reels.db.bak
git pull            # or copy the new files over
source venv/bin/activate && pip install -r requirements.txt   # no new dependencies
sudo cp systemd/reels-autopilot.service systemd/reels-web.service systemd/reels-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reels-watchdog
sudo systemctl restart reels-autopilot reels-web
journalctl -u reels-autopilot -f      # or tail logs/reels-autopilot.log
```

The dashboard restart buttons use `sudo -n systemctl restart …`; allow it without a password:

```
electro ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart reels-autopilot, /usr/bin/systemctl restart reels-web, /usr/bin/systemctl restart reels-watchdog
```

Logs live in `logs/`, runtime state in `state/`, per-account sessions in `sessions/`.
