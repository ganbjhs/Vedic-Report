"""Read a published Google Sheet as CSV. The project's ONLY outbound HTTP path.

Scope, deliberately narrow: **published / link-shared sheets via Google's CSV
export endpoint.** No OAuth, no private sheets, no Drive API, no credentials.
If a sheet is not readable without signing in, this says so and stops.

WHY IT IS WRITTEN THIS DEFENSIVELY. Before this file the repo made no outbound
requests at all — no `requests`, no `httpx`, not even `urllib` — so there was no
existing fetch path and no SSRF guard to inherit. A "fetch this URL for me"
endpoint behind a login is a classic pivot into a private network, so every
control here is deliberate:

  * https only, and the host must be on a two-entry allow-list;
  * redirects are followed manually and **re-validated at every hop**, because
    Google redirects large exports to googleusercontent.com;
  * the resolved IP must be public, so a DNS answer pointing at 169.254.169.254
    or 10.x is refused even if the hostname passes;
  * a byte cap enforced while streaming, not from Content-Length (which lies);
  * a content-type check, because a sheet that is not published returns an HTML
    sign-in page with 200 OK — parsing that as CSV would produce nonsense
    instead of "this sheet is not shared".

Nothing here validates X links. `input_loader.is_x_url` must NEVER be reused for
that: it is a substring test, so `https://evil.com/?q=x.com` passes it.

Standard library only — `requirements.txt` is frozen (RULEBOOK rule 1) and this
needs no dependency.
"""
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse, urlunparse

from . import config

ALLOWED_HOSTS = {"docs.google.com"}
# Google redirects a large CSV export to a hashed googleusercontent host.
ALLOWED_HOST_SUFFIXES = (".googleusercontent.com",)

MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 12          # seconds, covers connect + each read
MAX_BYTES = config.MAX_UPLOAD_BYTES
_CHUNK = 64 * 1024

_UA = "ReportAutomation/1.0 (+sheet-import)"

_DOC_ID = re.compile(r"/spreadsheets/d/(?:e/)?([A-Za-z0-9_-]{10,})")
_IS_PUBLISHED = re.compile(r"/spreadsheets/d/e/")
_GID_IN_FRAGMENT = re.compile(r"gid=(\d+)")


class SheetError(Exception):
    """A user-facing problem with a sheet URL or its contents."""


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #
def looks_like_sheet_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("http") and "docs.google.com/spreadsheets" in v


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if host in ALLOWED_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in ALLOWED_HOST_SUFFIXES)


def _assert_public_ip(host: str) -> None:
    """Refuse a hostname that resolves anywhere private.

    Belt and braces while the allow-list is exactly Google — but the allow-list
    is the sort of thing that gets widened later, and this is the control that
    still holds when it is.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SheetError(f"Could not resolve {host}.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise SheetError(
                f"{host} resolves to a non-public address ({ip}) — refusing.")


def _validate(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "https":
        raise SheetError("Only https:// sheet links are accepted.")
    if p.username or p.password:
        raise SheetError("That link contains credentials — remove them.")
    if p.port not in (None, 443):
        raise SheetError("Only the standard https port is allowed.")
    if not _host_allowed(p.hostname or ""):
        raise SheetError(
            f"Only Google Sheets links are accepted (got {p.hostname or '?'}). "
            "Paste the sheet's normal docs.google.com URL.")
    _assert_public_ip(p.hostname)
    return url


def export_url(url: str) -> str:
    """Turn any Google Sheets URL into its CSV export, preserving the tab."""
    raw = (url or "").strip()
    if not raw:
        raise SheetError("Paste a Google Sheets link.")
    _validate(raw)

    p = urlparse(raw)
    m = _DOC_ID.search(p.path)
    if not m:
        raise SheetError(
            "That does not look like a Google Sheets link. It should contain "
            "/spreadsheets/d/...")
    doc_id = m.group(1)

    gid = None
    q = parse_qs(p.query or "")
    if "gid" in q and q["gid"]:
        gid = q["gid"][0]
    elif p.fragment:
        fm = _GID_IN_FRAGMENT.search(p.fragment)
        if fm:
            gid = fm.group(1)
    if gid is not None and not gid.isdigit():
        raise SheetError("The tab id (gid) in that link is not a number.")

    if _IS_PUBLISHED.search(p.path):
        # "Published to the web" documents use a different export path.
        path = f"/spreadsheets/d/e/{doc_id}/pub"
        query = "output=csv" + (f"&gid={gid}" if gid else "")
    else:
        path = f"/spreadsheets/d/{doc_id}/export"
        query = "format=csv" + (f"&gid={gid}" if gid else "")

    return urlunparse(("https", "docs.google.com", path, "", query, ""))


def describe(url: str) -> dict:
    """Doc id + tab, for showing the user what was understood."""
    p = urlparse(export_url(url))
    q = parse_qs(p.query)
    m = _DOC_ID.search(p.path)
    return {"doc_id": (m.group(1) if m else "")[:24],
            "gid": (q.get("gid") or [None])[0],
            "published": bool(_IS_PUBLISHED.search(p.path))}


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def _open_no_redirect(url: str):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None                      # handled manually, see fetch_csv

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1"})
    return opener.open(req, timeout=CONNECT_TIMEOUT)


def fetch_csv(url: str) -> str:
    """Fetch the sheet's CSV. Raises SheetError with something a user can act on."""
    target = export_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        try:
            resp = _open_no_redirect(target)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("Location") or ""
                if not location:
                    raise SheetError("Google redirected without a destination.")
                target = _validate(urllib.parse.urljoin(target, location))
                continue
            if e.code in (401, 403):
                raise SheetError(
                    "That sheet is not shared publicly. In Google Sheets: "
                    "Share → General access → “Anyone with the link”, or "
                    "File → Share → Publish to web.") from e
            if e.code == 404:
                raise SheetError(
                    "No sheet found at that link — check the URL and the tab.") from e
            raise SheetError(f"Google returned HTTP {e.code} for that sheet.") from e
        except urllib.error.URLError as e:
            raise SheetError(f"Could not reach Google Sheets ({e.reason}).") from e
        except (TimeoutError, socket.timeout) as e:
            raise SheetError("Google Sheets did not respond in time.") from e

        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                # 200 OK with a sign-in page. Parsing this as CSV would invent
                # rows out of HTML instead of telling the truth.
                raise SheetError(
                    "That link returned a sign-in page, not data — the sheet "
                    "is not shared publicly. Set Share → “Anyone with the "
                    "link”, then try again.")
            if ctype and not any(t in ctype for t in
                                 ("text/csv", "text/plain",
                                  "application/octet-stream")):
                raise SheetError(f"Expected CSV but Google sent {ctype!r}.")

            chunks, size = [], 0
            while True:
                block = resp.read(_CHUNK)
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise SheetError(
                        f"That sheet is larger than the "
                        f"{config.MAX_UPLOAD_MB} MB limit.")
                chunks.append(block)

        raw = b"".join(chunks)
        if not raw.strip():
            raise SheetError("That sheet (or tab) is empty.")
        for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return raw.decode("utf-8", errors="replace")

    raise SheetError("Too many redirects while fetching that sheet.")
