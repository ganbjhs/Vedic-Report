# Moving Report Maker to the new server

Everything the tool needs is in two files:

| File | What it is | Where it comes from |
|---|---|---|
| `VedicReport-handover-*.zip` | the code (this folder) | given to you |
| `vedicreport-state-*.tgz` | logins, secrets, database, reports | **you create it on the old server** in step 2 |

The zip alone is NOT enough. The X login, the WhatsApp login, the Telegram bot
tokens, the job database and all generated reports live only on the old
server, so they have to be pulled off it. The two scripts in this folder do
that; nothing else is needed.

You need: root SSH to the **old** server (`200.97.175.12`) and to the **new**
server, and access to the DNS panel (Hostinger hPanel) for `vedictech.in`.
Budget about 20 minutes of downtime.

## 1. Put the code on the new server

```bash
scp VedicReport-handover-*.zip root@<NEW_IP>:~/
ssh root@<NEW_IP>
unzip -q VedicReport-handover-*.zip -d app     # -> ~/app
```

## 2. Pack the state on the old server  (tool goes offline here)

```bash
scp app/handover/backup-old-server.sh root@200.97.175.12:~/app/handover/   # from the new server, or your laptop
ssh root@200.97.175.12
cd ~/app && bash handover/backup-old-server.sh
```

The script stops the tool, packs everything into `~/vedicreport-state-<date>.tgz`
and prints a `scp` line. Run that line to copy the file to the new server:

```bash
scp ~/vedicreport-state-*.tgz root@<NEW_IP>:~/
```

Leave the old server stopped. Do **not** start it again unless you are rolling
back (see bottom).

## 3. Point the domain at the new server

In hPanel → DNS for `vedictech.in`, edit the **A record** `report` from
`200.97.175.12` to `<NEW_IP>`. Set TTL to 300 if you can.

## 4. Start on the new server

```bash
ssh root@<NEW_IP>
cd ~/app && bash handover/setup-new-server.sh ~/vedicreport-state-*.tgz
```

Installs Docker if needed, unpacks the state, picks `WORKERS` for the RAM on the
machine, builds, starts everything, waits for the health checks, then prints
what to check. If the RAM-based choice isn't what you want:

```bash
bash handover/setup-new-server.sh ~/vedicreport-state-*.tgz --workers 5
```

## 5. Check

```bash
docker compose --profile caddy logs -f caddy      # wait for "certificate obtained"
```

Then in a browser: `https://report.vedictech.in` (log in, run one small report)
and `https://report.vedictech.in/wa`. In Telegram, send the bot a message.

Two things that can need a hand after an IP change:

- **X asks to log in again** — the copied cookie usually works, but X sometimes
  challenges a new IP. Follow README → "4. Seed the X login".
- **WhatsApp asks to re-link** — open `/wa`, scan the QR once.

## Rolling back

Only if the new server does not work: on the old server
`cd ~/app && docker compose --profile caddy --profile splitter start`, and put
the DNS A record back to `200.97.175.12`. Nothing on the old server was
deleted, so this is safe for as long as it exists.

## After a few good days

Cancel the old server. In the repo, the old IP is still mentioned in comments
in `Caddyfile`, `README.md` and `wa/DEPLOY.md` — update them when convenient.
Later deploys go back to the normal way (`git pull` + `./deploy.sh`) once the
new server has been given the git remote:

```bash
cd ~/app && git init -q && git remote add origin https://github.com/ganbjhs/Vedic-Report.git
git fetch -q origin && git checkout -q -f -b main origin/main   # same code; .env, data/ etc. are untouched
```
