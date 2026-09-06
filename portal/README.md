# Client Portal

The client-facing side of VedicReport: a client signs in at its own hostname
and sees a daily social report — one day at a time, published two days after
the sheet's date — with trend graphs per category and platform, a growth
trend, a feed of the posts, and an Excel export. Plan and rationale:
[`docs/client-portal-plan.md`](../docs/client-portal-plan.md).

It is a **separate app** (`portal/`), a separate container, a separate cookie
and secret. It shares exactly one thing with Report Maker: `data/portal.db`,
which Report Maker writes and the portal reads.

```
Report Maker (webapp/)                      Client Portal (portal/)
─────────────────────                       ───────────────────────
run finishes ──▶ portal_publish.publish_run ──▶ post_metrics  ◀── /api/daily, /api/trend
Admin → Clients ─▶ clients, users, sources ─┘        ▲
scraper sync ───▶ captions, media, counts ──────────┘  (read-only connection)
```

## Run it locally

```bash
.venv/bin/pip install -r requirements-portal.txt
# .env: PORTAL_SESSION_SECRET, PORTAL_KEY_SECRET (two long random strings),
#       PORTAL_PUBLIC_URL=http://127.0.0.1:8020
.venv/bin/python -m uvicorn portal.main:app --port 8020 --reload
```

Report Maker (port 8000) and the portal (port 8020) run side by side and
both open `data/portal.db`; the file is created on first start.

Nothing to sign in with yet? A fresh `portal.db` has no clients and no users.
For a look at the dashboard without going through Report Maker:

```bash
.venv/bin/python scripts/portal_dev_seed.py --email you@example.com --password 'Portal-Demo-1234'
.venv/bin/python scripts/portal_dev_seed.py --remove      # take the demo client out again
```

That creates a *Demo Client* with 30 days of sample posts and a password
sign-in (no invite step). Local only — it refuses to run when
`PORTAL_COOKIE_SECURE=1`.

## First client, step by step

1. Report Maker → **Admin → Clients → New client**. Name it, set the delay
   (2 days), an accent colour if you like.
2. Tick the **projects** that feed this client and *Save links*. Every
   finished run of those projects is published immediately (that is the
   backfill); every run after that publishes itself when it finishes.
3. **Categories** now lists the sheet's headings. Rename them for the client,
   set an order, hide the ones that are internal (e.g. *Counter Comments*).
4. **Invite** the client's e-mail. Copy the link — it is shown once, works
   once, for 72 hours — and send it. They set a password and land on the
   report.
5. Optional: **Data source** — the scraper's URL (with `{from}`/`{to}` or
   `{date}`), how it takes the key, and the key itself. *Sync now* fetches
   the last few days; the scheduler then syncs every `PORTAL_SYNC_MINUTES`.
   Captions, media and fresh counts appear in the feed and the numbers.

The command-line equivalent of step 2 for every client:
`.venv/bin/python scripts/portal_backfill.py` (add `--sync` for step 5).

## Deploy

```bash
# .env on the server — same PORTAL_KEY_SECRET for both containers
PORTAL_PUBLIC_URL=https://clients.vedictech.in
PORTAL_SESSION_SECRET=…   PORTAL_KEY_SECRET=…   PORTAL_COOKIE_SECURE=1

docker compose up -d --build portal        # the Caddyfile already routes clients.vedictech.in
```

DNS: an A record for `clients.vedictech.in` → the server. Caddy issues the
certificate on first start.

## The two rules that keep clients apart

* `portal/db.py: visible_posts()` is the only reader of `post_metrics`, and
  it always adds `client_id = <session's client> AND visible_from <= today`.
  There is no client id in any URL.
* The portal opens the database `mode=ro` for everything but its own auth
  tables. A bug in a query cannot become a write.

## Tests

```bash
.venv/bin/python portal/tests/test_portal.py
```

No browser, no network: url normalisation and count parsing, sealed keys,
the scraper adapter on a sample body, the publish step from a fake
`results.json`, the two-day rule, category mapping, tenant isolation, and
(with FastAPI installed) invite → login → API → export → logout.

## Files

| | |
|---|---|
| `schema.py` `util.py` `secretbox.py` `scraper.py` | leaf modules, stdlib only — the only ones `webapp/` imports |
| `config.py` `db.py` `auth.py` `queries.py` `export.py` `routes.py` `main.py` | the app |
| `templates/` `static/` | login, invite, the dashboard; one CSS, one JS |
| `../webapp/portal_publish.py` | publish, scraper sync, backfill, scheduler |
| `../webapp/routes_clients.py` + `templates/clients.html` | Admin → Clients |
| `../scripts/portal_backfill.py` | publish every finished run from the command line |
| `../Dockerfile.portal` `../requirements-portal.txt` | the image |

The scraper's field names are matched loosely through `scraper.FIELD_MAP`;
when a real response looks different, add the name there — nothing else
changes.
