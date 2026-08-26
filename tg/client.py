"""Report Maker API client for the Telegram bot.

The bot has no privileges of its own: it signs in as an ordinary APP_USERS
account and then uses exactly the endpoints the browser uses. Nothing in
`webapp/` had to change for this to work, and when the scoped `/v1` token
layer lands only this file is replaced.

Auth shape (see webapp/auth.py):
  * POST /login sets the signed `ra_session` cookie.
  * The CSRF token lives in that session and is rendered into every page as a
    hidden `csrf_token` input. login_session() rotates it, so it is scraped
    from `/` *after* signing in, never from the login page.
"""
from __future__ import annotations

import re
from urllib.parse import unquote
from typing import Iterable

import httpx

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

# login.html renders a refusal as <div class="alert alert-error">…</div>.
# Reading it back means the user sees the server's real reason instead of a
# guess made on this side.
_LOGIN_ERR_RE = re.compile(
    r'class="alert alert-error"[^>]*>\s*(.*?)\s*</div>', re.S)

# Deliberately loose: people paste links inside sentences, with trailing
# punctuation, in brackets, or several to a line — Telegram hands the text
# back exactly as typed and none of it is cleaned up for us.
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)

# The same links pasted without a scheme. Very common from mobile share sheets
# and from anyone retyping a link. Restricted to hosts we actually capture, so
# an ordinary sentence containing a full stop is never mistaken for a URL.
_BARE_RE = re.compile(
    r'(?<![\w/@.])((?:www\.)?(?:'
    r'x\.com|twitter\.com|instagram\.com|facebook\.com|fb\.watch|m\.facebook\.com|'
    r'youtube\.com|youtu\.be|threads\.net|linkedin\.com|tiktok\.com|reddit\.com'
    r')/[^\s<>"\')\]]+)', re.I)

# Trailing characters that are punctuation in a sentence, never part of a link.
_TRIM = ".,;:!?\u2026'\"))]}>\u201d\u2019"

_X_HOSTS = ("twitter.com", "x.com")


class ApiError(RuntimeError):
    """A refusal the user should read — the server's own wording is kept."""


def find_links(text: str) -> list:
    """Every post URL in a blob of text, order kept, duplicates removed.

    Handles the shapes people actually send: several links on one line, links
    wrapped in a sentence, links in brackets, links with a trailing full stop,
    and links pasted without `https://`.
    """
    text = text or ""
    out, seen = [], set()

    def add(url: str):
        url = url.strip().rstrip(_TRIM)
        # A closing bracket only belongs to the URL if it was opened inside it
        # — "(see https://x.com/a/status/1)" must not keep the paren.
        while url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1].rstrip(_TRIM)
        if not url:
            return
        key = url.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(url)

    spans = []
    for m in _URL_RE.finditer(text):
        spans.append(m.span())
        add(m.group(0))
    for m in _BARE_RE.finditer(text):
        # Skip anything already inside a full URL we just took.
        if any(a <= m.start() < b for a, b in spans):
            continue
        add("https://" + m.group(1))
    return out


def guess_platform(links: Iterable[str]) -> str:
    """`x` when every link is a tweet, else `combined`.

    Asking the user which platform their own links are on is a question whose
    answer is already in the data — the rule this bot is built on.
    """
    links = list(links)
    if links and all(any(h in url for h in _X_HOSTS) for url in links):
        return "x"
    return "combined"


class ReportMaker:
    """One signed-in session against a Report Maker instance."""

    def __init__(self, base_url: str, username: str, password: str,
                 timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.csrf = ""
        self._http = httpx.AsyncClient(base_url=self.base, timeout=timeout,
                                       follow_redirects=True)

    async def aclose(self):
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    async def login(self) -> None:
        page = await self._http.get("/login")
        m = _CSRF_RE.search(page.text)
        if not m:
            raise ApiError(f"{self.base} did not look like Report Maker "
                           "(no login form found).")
        r = await self._http.post("/login", data={
            "username": self.username, "password": self.password,
            "csrf_token": m.group(1), "next": "/"})
        if r.status_code >= 400 or str(r.url).rstrip("/").endswith("/login"):
            m = _LOGIN_ERR_RE.search(r.text)
            said = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
            hint = ""
            if "expired" in said.lower() and self.base.startswith("http://"):
                # COOKIE_SECURE=1 marks the session cookie Secure. Browsers make
                # an exception for localhost; HTTP clients do not, so the cookie
                # never comes back and every CSRF check fails as "expired".
                hint = ("  The session cookie never came back — set "
                        "COOKIE_SECURE=0 in the app's .env while APP_URL is "
                        "http://, or point APP_URL at the https address.")
            raise ApiError(
                (f"Report Maker said: {said}" if said else
                 f"Report Maker refused the sign-in (HTTP {r.status_code}).")
                + f"  [user {self.username!r} at {self.base}]" + hint)
        m = _CSRF_RE.search(r.text)
        if not m:
            m = _CSRF_RE.search((await self._http.get("/")).text)
        if not m:
            raise ApiError("Signed in, but no CSRF token was found on the "
                           "dashboard — is this a supported Report Maker build?")
        self.csrf = m.group(1)

    async def _post(self, path: str, *, data=None, files=None, retry=True):
        """POST with the CSRF token, re-signing in once if the session died."""
        if not self.csrf:
            await self.login()
        body = dict(data or {})
        body["csrf_token"] = self.csrf
        r = await self._http.post(path, data=body, files=files)
        if r.status_code == 401 and retry:
            # The cookie expired. One silent recovery, then the real error.
            await self.login()
            return await self._post(path, data=data, files=files, retry=False)
        return r

    async def _get(self, path: str, retry=True, **kw):
        r = await self._http.get(path, **kw)
        if r.status_code == 401 and retry:
            await self.login()
            return await self._get(path, retry=False, **kw)
        return r

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            return str(r.json().get("detail") or r.text)[:400]
        except Exception:
            return (r.text or f"HTTP {r.status_code}")[:400]

    # ------------------------------------------------------------------ #
    # Project + styles
    # ------------------------------------------------------------------ #
    async def projects(self) -> tuple:
        """(all projects, the dashboard's current one). The list carries no
        styles — `project()` fetches those for the one that gets picked."""
        r = await self._get("/api/projects")
        if r.status_code != 200:
            raise ApiError(self._detail(r))
        body = r.json() or {}
        return (body.get("projects") or []), (body.get("current") or {})

    async def project(self, pid: str) -> dict:
        """One project, styles included."""
        r = await self._get(f"/api/projects/{pid}")
        if r.status_code != 200:
            raise ApiError(self._detail(r))
        return r.json() or {}

    async def current_project(self) -> dict:
        r = await self._get("/api/projects")
        if r.status_code != 200:
            raise ApiError(self._detail(r))
        return (r.json() or {}).get("current") or {}

    # ------------------------------------------------------------------ #
    # Preview — what WOULD be captured, costing nothing
    # ------------------------------------------------------------------ #
    async def preview(self, *, text: str = "", file: tuple = None,
                      platform: str = "x", dedupe: bool = True) -> dict:
        files = {"file": file} if file else None
        r = await self._post("/api/preview", data={
            "text": text, "platform": platform,
            "dedupe": "1" if dedupe else ""}, files=files)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        if r.status_code != 200 or not body.get("ok"):
            raise ApiError(body.get("detail") or self._detail(r))
        return body

    # ------------------------------------------------------------------ #
    # Submit + poll + download
    # ------------------------------------------------------------------ #
    async def submit(self, *, report_name: str, report_type: str,
                     platform: str, text: str = "", file: tuple = None,
                     outputs: list = None, project_id: str = "",
                     dedupe: bool = True, _retry: bool = True) -> dict:
        if not self.csrf:
            await self.login()
        # httpx treats a LIST `data=` as raw content and builds a sync-only
        # stream, which an AsyncClient then refuses to send. Repeated form
        # fields (`report_type`, `outputs` — both List[str] on the server) go
        # as dict values instead.
        data = {"report_name": report_name,
                "report_type": [report_type] if isinstance(report_type, str)
                               else list(report_type),
                "platform": platform,
                "text": text,
                "dedupe": "1" if dedupe else "",
                "csrf_token": self.csrf}
        if project_id:
            data["project_id"] = project_id
        if outputs:
            data["outputs"] = list(outputs)
        files = {"file": file} if file else None
        r = await self._http.post("/api/jobs", data=data, files=files)
        if r.status_code == 401 and _retry:
            await self.login()
            return await self.submit(
                report_name=report_name, report_type=report_type,
                platform=platform, text=text, file=file, outputs=outputs,
                project_id=project_id, dedupe=dedupe, _retry=False)
        if r.status_code not in (200, 202):
            raise ApiError(self._detail(r))
        return r.json()

    async def status(self, job_id: str) -> dict:
        r = await self._get(f"/api/jobs/{job_id}")
        if r.status_code != 200:
            raise ApiError(self._detail(r))
        return r.json()

    async def download(self, job_id: str, kind: str) -> tuple:
        """(filename, bytes) for one artifact."""
        r = await self._get(f"/api/jobs/{job_id}/download/{kind}")
        if r.status_code != 200:
            raise ApiError(self._detail(r))
        name = f"report.{kind}"
        disp = r.headers.get("content-disposition", "")
        # Starlette sends RFC 5987 (`filename*=utf-8\'\'July%20report.pdf`)
        # whenever the name is not plain ASCII — a space is enough. Read that
        # form first and percent-decode it, or the charset lands in the name.
        m = re.search(r"filename\*=\s*([\w-]+)''([^;]+)", disp, re.I)
        if m:
            name = unquote(m.group(2))
        else:
            m = re.search(r'filename="?([^";]+)"?', disp, re.I)
            if m:
                name = m.group(1).strip()
        return name, r.content
