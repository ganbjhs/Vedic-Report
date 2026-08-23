# Deploying WA Toolkit at `https://report.vedictech.in/wa`

The desktop app still works exactly as before (`python app.py`). This document
covers the **server build** only.

---

## What had to change, and why

WA Toolkit was built as a **pywebview desktop app**: `app.py` opens a native OS
window and the React UI reaches Python through `window.pywebview.api`, a bridge
that only exists inside that window. There was no HTTP server and no port, so it
could not be reverse-proxied as-is — served from a web server the page would
have loaded and then hung forever waiting for a `pywebviewready` event that
never fires.

Five changes, all inside `wa/`. The report tool's code was not touched.

| File | Change |
|---|---|
| `server.py` | **New.** FastAPI app that reuses the *same* `Worker` and `Api` from `app.py` and swaps the transport: `window.pywebview.api.foo(a,b)` → `POST ./api/foo {"args":[a,b]}`. Adds `/upload`, `/qr.png`, `/health`. |
| `app.py` | `import webview` made optional, so the module imports on a server with no pywebview. `main()` refuses to run without it. Desktop behaviour identical. |
| `wa/paths.py` | New `WA_DATA_DIR` env override, so config/tasks/`wa_profile` land on a writable volume instead of the read-only image. `ensure_defaults()` now triggers whenever `DATA_DIR != APP_DIR` (frozen app *or* server); a plain source run is still a no-op. |
| `ui-src/app.jsx` | The 8-line bridge became a `fetch()` wrapper. `pick_file()` (native dialog) became a real `<input type=file>` posting to `/upload`. New `QrPanel` overlay for login. |
| `ui/bundle.js`, `ui/bundle.css` | Rebuilt from the above with `ui-src/build.sh`. |

Nothing in `wa/session.py`, `chat.py`, `reader.py`, `links.py`, `sheets.py`,
`metrics.py` or `engine.py` changed. `bot.py` and `wa.py` (CLI) are untouched.

## Login: no login of its own

Caddy serves this under the same hostname as Report Maker, so the browser
already sends the `ra_session` cookie. `server.py` mounts Starlette's
`SessionMiddleware` with the **same `SESSION_SECRET`** out of the same `.env`
and *reads* that cookie:

* signed out → `303 /login?next=/wa/` (Report Maker's own login page)
* signed in → the toolkit
* API calls signed out → `401`, and the UI bounces to `/login`

It only ever **reads** the session dict, so Starlette emits no `Set-Cookie` and
Report Maker's own session is left byte-identical. A wrong or missing
`SESSION_SECRET` fails closed — the cookie will not verify and everyone is sent
to the login page.

Anyone in `APP_USERS` can reach `/wa`. There is no separate role check; if you
want it restricted to admins, say so and it is a two-line change in `server.py`.

---

## Deploy

### A — on your Mac: push only this work

You have 13 unrelated modified files in `webapp/`, `src/` and `profiles/`. Stage
by path so none of them ride along.

```bash
cd ~/Desktop/Project/VedicReport

git status --short                      # look before you leap
git add wa Caddyfile docker-compose.yml
git status --short                      # the 13 others must still show as ' M'

git commit -m "wa: serve WA Toolkit at /wa behind the Report Maker login"
git push origin main
```

`wa/.gitignore` keeps `wa_profile/` (your live WhatsApp login, 101 MB),
`checkpoints/`, `node_modules/` and `data/` out of the commit — 46 files, ~536 KB
go up. Verify before pushing if you like:

```bash
git show --stat --name-only HEAD | grep -c wa_profile     # must print 0
```

### B — on the server

```bash
ssh root@200.97.175.12
cd ~/app

git pull --ff-only origin main

./wa/deploy-wa.sh
```

`deploy-wa.sh` creates `wa/data`, chowns it to UID 1000, builds and starts **only**
the `wa` service, waits for its health check, validates the Caddyfile, then hot-
reloads Caddy. The report tool is never stopped or rebuilt.

<details>
<summary>The same thing by hand, if you would rather watch each step</summary>

```bash
cd ~/app

# 1. the volume that holds the WhatsApp login + config. UID 1000 = the
#    container's user; root-owned here means "cannot write config.json".
mkdir -p wa/data && chown -R 1000:1000 wa/data

# 2. build and start ONLY wa. Naming the service is what keeps `web` untouched —
#    a bare `docker compose up -d` would recreate the report tool too.
docker compose build wa
docker compose up -d wa

# 3. is it alive?
curl -s localhost:8010/health          # {"ok":true,"status":"idle","browser":false}
docker compose logs --tail=30 wa

# 4. check the new Caddyfile BEFORE applying it — a syntax error here would
#    take the report tool down with it.
docker compose --profile caddy exec caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# 5. apply it. `reload` is zero-downtime; do NOT `restart` caddy.
docker compose --profile caddy exec caddy \
  caddy reload --config /etc/caddy/Caddyfile
```
</details>

### C — verify

```bash
# report tool: unchanged, still 200 (or 303 to /login)
curl -sI https://report.vedictech.in/ | head -1

# /wa redirects to /wa/ ...
curl -sI https://report.vedictech.in/wa | head -2

# ... and /wa/ sends a signed-out visitor to the Report Maker login
curl -sI https://report.vedictech.in/wa/ | head -2      # 303, Location: /login?next=/wa/
```

If `curl localhost:8010/health` works but `/wa` 502s, Caddy did not reload —
re-run step 5. If Caddy is not running at all (`docker compose ps` shows no
caddy container), this server fronts the app with something else; tell me and
the nginx equivalent is three lines.

### One-time WhatsApp login

The server has no screen, so the QR cannot be scanned off a Chromium window.
Instead `server.py` runs Chromium headless and screenshots the login page every
two seconds; the UI shows it as an overlay.

1. Open `https://report.vedictech.in/wa` and sign in with your Report Maker login.
2. Click **Open WhatsApp** (bottom left).
3. The QR overlay appears. On your phone: **Settings → Linked devices → Link a device**.
4. The overlay closes by itself once WhatsApp confirms. The login is saved in
   `./wa/data/wa_profile/` on the server and survives rebuilds.

You do not need to copy your Mac's `wa_profile/` up — and you should not: it is
a live credential, `.dockerignore` and `wa/.gitignore` both exclude it, and a
macOS Chromium profile is not reliably portable to Linux anyway.

---

## Rollback

```bash
docker compose stop wa && docker compose rm -f wa
cp Caddyfile.bak Caddyfile
cp docker-compose.yml.bak docker-compose.yml
docker compose --profile caddy up -d caddy
```

`Caddyfile.bak` and `docker-compose.yml.bak` are the originals, saved before
editing. The report tool never stops during any of this.

---

## Known limits

* **One user at a time.** One Chromium per profile directory. A second person
  clicking around while a job runs will fight over the same browser. The bot and
  the app still cannot both hold the session, same as on the desktop.
* **The bot spawns `bot.py` as a subprocess** inside the container. It works, but
  it dies with the container — it is not a separate service.
* **`Worker._session()` guards against the bot holding the browser by checking
  `Api._bot`, a class attribute, while `bot_start` sets `self._bot`, an instance
  attribute.** So that guard never fires. This is pre-existing, present in the
  desktop app too, and was left alone.
* **WhatsApp ban risk.** Automating WhatsApp Web from a datacenter IP, and moving
  an existing session from a home connection in India to a VPS, is a meaningfully
  higher-risk profile than running on your laptop. Use a number you can afford to
  lose.
