# New server — information request

Prompt for the agent that has knowledge of / access to the new server.
Copy everything below the line and send it as-is.

---

You are assisting with the migration of a Dockerized web application (VedicReport)
from an existing 1-vCPU VPS to a larger server that you have knowledge of or access to.
Provide the information below. Keep answers factual and in the exact format requested.

## SECTION A — SERVER ACCESS

- A1. Public IP address of the new server
- A2. SSH port (if not 22)
- A3. Root login: password, or an SSH private key that is authorized for root
- A4. Hosting provider / control panel (e.g. Hostinger hPanel, DigitalOcean, AWS, self-hosted)

If you can retrieve these directly, provide them.
If you cannot, give the server owner short step-by-step instructions to obtain them,
for example (Hostinger): hPanel → VPS → select the server → Overview shows the IP;
Settings → SSH access / Root password → set or reset the root password.
For other providers, give the equivalent path in that provider's panel.

## SECTION B — SERVER CAPACITY

Run the following on the new server and paste the raw output:

```bash
nproc
free -g
df -h /
cat /etc/os-release | head -3
uname -m
```

This determines how many parallel browser workers the application can run
(each worker needs roughly 1–1.5 GB RAM and one CPU core). The application
requires Ubuntu/Debian on x86_64 or arm64.

## SECTION C — EXISTING USAGE ON THE SERVER (conflict check)

Run and paste the output:

```bash
docker --version 2>/dev/null || echo "docker not installed"
ss -tlnp | grep -E ':80 |:443 |:8000 |:8010 |:8020 '
```

State whether any other application is already running on this server.
The migration needs ports 80 and 443 free, since it runs its own reverse proxy (Caddy).

## SECTION D — DOMAIN / DNS

The application is served at `report.vedictech.in`, which currently points to the old
server (`200.97.175.12`). The same domain will be reused; only its DNS A record must
be changed to the new server's IP.

- D1. Where is DNS for `vedictech.in` managed (registrar or DNS provider name)?
- D2. Can you change the A record for `report.vedictech.in` yourself?
  - If yes: confirm, and wait for instruction before changing it.
  - If no: give the server owner step-by-step instructions to change the A record
    for `report.vedictech.in` to the new IP in that provider's DNS panel.
- D3. Is a firewall configured at the provider level (outside the OS)?
  If yes, confirm that inbound ports 22, 80 and 443 are allowed, or explain how to allow them.

## RESPONSE FORMAT

```
A1 IP:                ______
A2 SSH port:          ______
A3 Root access:       password / key (provide it, or the steps to obtain it)
A4 Provider/panel:    ______
B  Command output:    (paste)
C  Command output:    (paste) + note on other running applications
D1 DNS provider:      ______
D2 A record access:   yes / no (+ steps if no)
D3 Provider firewall: none / configured (ports 22, 80, 443 status)
```

Do not provide anything beyond the items above.
