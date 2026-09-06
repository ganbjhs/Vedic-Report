# VedicReport — server & deploy runbook

Server: Hostinger VPS, `root@srv1838786`, app at **`/root/app`**, run with **docker compose**.
Repo: `github.com/ganbjhs/Twitter-Report`, branch **`feat/splitter-and-v1`**.
Web app IP (from Caddyfile): **200.97.175.12** (use this as `VPS_HOST`, or your SSH host).

---

## 0. First, push the clean-up commit

The server now has commit `5163b20` (pins `playwright==1.61.0`, gitignores runtime dirs), 1 ahead of origin. Make origin the source of truth so deploys are predictable:

```bash
# on the SERVER (needs your GitHub credentials / token)
cd /root/app
git push origin feat/splitter-and-v1        # fast-forward, no conflict
```
Then in your **local** repo, pull it so local and server match:
```bash
cd ~/Desktop/Project/VedicReport
git stash          # if you have local edits to requirements.txt
git pull origin feat/splitter-and-v1
git stash drop     # the pin is now in the pulled commit
```

---

## 1. Push-to-deploy — choose ONE

### Option A (recommended): GitHub Actions → SSH into the server

Add this file to the repo at **`.github/workflows/deploy.yml`** (it's currently empty on the server):

```yaml
name: Deploy to VPS
on:
  push:
    branches: [ feat/splitter-and-v1, main ]
  workflow_dispatch: {}          # lets you deploy manually from the Actions tab
concurrency: deploy-vps          # never run two deploys at once
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: SSH deploy
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          envs: GITHUB_REF_NAME
          script: |
            set -e
            cd /root/app
            git fetch --all --prune
            git reset --hard "origin/${GITHUB_REF_NAME}"
            docker compose up -d --build web
            docker image prune -f
```

**Setup (one time):**
```bash
# 1) on your Mac: make a deploy key
ssh-keygen -t ed25519 -f vps_deploy -N "" -C "github-actions-deploy"

# 2) authorise it on the server (paste vps_deploy.pub)
ssh root@200.97.175.12 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys' < vps_deploy.pub

# 3) in GitHub → repo → Settings → Secrets and variables → Actions → New secret, add:
#    VPS_HOST    = 200.97.175.12
#    VPS_USER    = root
#    VPS_SSH_KEY = <contents of the private file vps_deploy>
```
Now every push to `feat/splitter-and-v1` (or `main`) rebuilds and restarts the `web` container on the server. Note: `git reset --hard origin/...` makes **origin the source of truth** — always push your changes, don't edit files directly on the server (except `.env`, which is gitignored and stays).

### Option B (what "set a remote that pushes to the server" literally means): direct git push → server

```bash
# on the SERVER — a bare repo with a deploy hook
git init --bare /root/app.git
cat > /root/app.git/hooks/post-receive <<'EOF'
#!/bin/bash
set -e
GIT_WORK_TREE=/root/app git checkout -f
cd /root/app && docker compose up -d --build web && docker image prune -f
EOF
chmod +x /root/app.git/hooks/post-receive

# on your Mac — add the server as a git remote and push to deploy
git remote add server ssh://root@200.97.175.12/root/app.git
git push server feat/splitter-and-v1        # this deploys
```
With Option B you deploy by `git push server`, independent of GitHub. (You can keep `origin` = GitHub for backup and use `server` for deploys.) Don't use A and B at the same time on the same branch, or they fight over `/root/app`.

---

## 2. "Update requirements so it doesn't happen again"

Two different recurrences — keep them straight:

- **The OOM / stall (today's outage)** is prevented by **`.env`**, not requirements: `WORKERS=1`, `MAX_WORKERS=1` (a 1-vCPU box runs exactly one browser). Already set on the server and backed up as `.env.bak.*`. `.env` is gitignored, so it lives only on the server — **re-set it if you ever rebuild the VPS.** To make the *default* safe in code too, you could lower the default in `webapp/config.py`, but the `.env` value already wins.
- **The Playwright version drift** is fixed in **`requirements.txt`**: pinned `playwright==1.61.0` to match the Dockerfile base `mcr.microsoft.com/playwright/python:v1.61.0-noble` (committed in `5163b20`). The Dockerfile's rule is *"the base image tag MUST match the Playwright version"* — keep them equal whenever you bump either. To apply the pin to the **running** image, rebuild once:
  ```bash
  cd /root/app && docker compose build web && docker compose up -d web
  ```
  (Not urgent — the current image runs fine — but it removes the drift.)

`requirements-web.txt` and `requirements-portal.txt` are already range-pinned and fine.

---

## 3. Health & recovery cheatsheet

```bash
docker ps                                   # all 4 (web, wa, tg, tg-splitter) Up
curl -s localhost:8000/health               # {"ok":true,"mode":"queue"}
free -m                                      # during a capture, ~3.0 GB used is normal at WORKERS=1
docker exec app-web-1 printenv WORKERS       # must be 1
dmesg -T | grep -c 'Out of memory'           # should stop increasing
# a report failed with "Stalled"? it's almost always memory — confirm WORKERS=1, then Resume
```

Today's finished report is on the server at `/root/app/reports/5-9-26_Daily_Tweet_Report.pdf` (+ `.docx`).

---

## Client Portal — first deploy (added)

The portal ships as its own container (`portal` service) and is served at
**`https://report.vedictech.in/portal`** — no new subdomain / DNS needed.
Three things on the **server** (all gitignored, so they don't arrive with a pull):

1. Add to the server `.env` (same file the web app uses):
   ```
   PORTAL_BASE_PATH=/portal
   PORTAL_PUBLIC_URL=https://report.vedictech.in/portal
   PORTAL_SESSION_SECRET=<python3 -c 'import secrets;print(secrets.token_urlsafe(48))'>
   PORTAL_KEY_SECRET=<another one>
   PORTAL_COOKIE_SECURE=1
   ```
   Without `PORTAL_BASE_PATH=/portal` the app emits root-absolute links and the
   subpath breaks; without `PORTAL_SESSION_SECRET` clients are signed out on
   every restart.
2. `deploy.sh` now creates `data/portal.db` and health-checks the portal, so a
   plain push-to-deploy brings the container up. Verify after:
   ```
   docker compose ps portal && curl -s -o /dev/null -w '%{http_code}\n' localhost:8020/login
   ```
3. First client: **report tool → Admin → Clients → New client**, tick its
   projects, then **Invite** the client's e-mail. Finished runs of those
   projects publish to the portal automatically.

To move it onto its own subdomain later: point `clients.vedictech.in` at the
server, clear `PORTAL_BASE_PATH` (and set `PORTAL_PUBLIC_URL` to the subdomain),
redeploy. The `clients.vedictech.in` block is already in the Caddyfile.
