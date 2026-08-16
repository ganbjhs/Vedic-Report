#!/usr/bin/env python3
"""Google Sheets import: URL handling and the SSRF guards.

This is the project's only outbound HTTP path, added behind a login, so the
guards matter more than the feature. Everything here runs offline — the network
layer is exercised through a fake opener, so no request ever leaves the machine
and no Google quota is touched.

    .venv/bin/python profiles/tests/test_sheets.py
"""
import io
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from webapp import sheets, uploads          # noqa: E402

FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))
    if not ok:
        FAILS.append(name)


def refuses(name, url, needle=""):
    try:
        sheets.export_url(url)
    except sheets.SheetError as e:
        ok = needle.lower() in str(e).lower()
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {str(e)[:78]}")
        if not ok:
            FAILS.append(name)
        return
    print(f"  FAIL  {name}: ACCEPTED a URL it should refuse")
    FAILS.append(name)


# --------------------------------------------------------------------------- #
print("\n1. real sheet URLs become the right CSV export")
DOC = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-x"
check("edit link + #gid",
      sheets.export_url(f"https://docs.google.com/spreadsheets/d/{DOC}/edit#gid=42"),
      f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=csv&gid=42")
check("edit link, no gid -> first tab",
      sheets.export_url(f"https://docs.google.com/spreadsheets/d/{DOC}/edit"),
      f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=csv")
check("?gid= in the query",
      sheets.export_url(f"https://docs.google.com/spreadsheets/d/{DOC}/edit?gid=7"),
      f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=csv&gid=7")
check("already an export link is normalised, not doubled",
      sheets.export_url(
          f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=csv&gid=3"),
      f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=csv&gid=3")
check("published-to-web uses the pub path",
      sheets.export_url(
          f"https://docs.google.com/spreadsheets/d/e/{DOC}/pubhtml?gid=9"),
      f"https://docs.google.com/spreadsheets/d/e/{DOC}/pub?output=csv&gid=9")
check("describe() reports the tab",
      sheets.describe(
          f"https://docs.google.com/spreadsheets/d/{DOC}/edit#gid=5")["gid"], "5")

# --------------------------------------------------------------------------- #
print("\n2. SSRF: anything off the allow-list is refused")
refuses("plain http", f"http://docs.google.com/spreadsheets/d/{DOC}/edit", "https")
refuses("another host", "https://evil.example.com/spreadsheets/d/x/edit", "only google")
refuses("localhost", "https://127.0.0.1/spreadsheets/d/x/edit", "only google")
refuses("AWS metadata IP", "https://169.254.169.254/spreadsheets/d/x/edit", "only google")
refuses("file scheme", "file:///etc/passwd", "https")
refuses("gopher scheme", "gopher://docs.google.com/x", "https")
refuses("credentials in the URL",
        f"https://user:pw@docs.google.com/spreadsheets/d/{DOC}/edit", "credentials")
refuses("non-standard port",
        f"https://docs.google.com:8080/spreadsheets/d/{DOC}/edit", "port")
refuses("lookalike host suffix",
        f"https://docs.google.com.evil.net/spreadsheets/d/{DOC}/edit", "only google")
refuses("google but not sheets", "https://docs.google.com/document/d/abc/edit",
        "does not look like")
refuses("empty", "", "paste a google sheets link")
refuses("non-numeric gid",
        f"https://docs.google.com/spreadsheets/d/{DOC}/edit?gid=DROP", "not a number")

print("\n   host allow-list behaves")
check("docs.google.com allowed", sheets._host_allowed("docs.google.com"), True)
check("googleusercontent subdomain allowed (large exports redirect there)",
      sheets._host_allowed("doc-0g-1c-sheets.googleusercontent.com"), True)
check("bare googleusercontent.com NOT allowed",
      sheets._host_allowed("googleusercontent.com"), False)
check("suffix-spoof refused",
      sheets._host_allowed("evilgoogleusercontent.com.attacker.net"), False)
check("uppercase host still matched", sheets._host_allowed("DOCS.GOOGLE.COM"), True)

# --------------------------------------------------------------------------- #
print("\n3. private-IP resolution is refused even for an allowed host")
real_getaddrinfo = sheets.socket.getaddrinfo
try:
    sheets.socket.getaddrinfo = lambda *a, **k: [
        (2, 1, 6, "", ("10.0.0.5", 443))]
    try:
        sheets._assert_public_ip("docs.google.com")
        check("private IP refused", "accepted", "raised")
    except sheets.SheetError as e:
        check("private IP refused", "non-public" in str(e), True)
    sheets.socket.getaddrinfo = lambda *a, **k: [
        (2, 1, 6, "", ("169.254.169.254", 443))]
    try:
        sheets._assert_public_ip("docs.google.com")
        check("metadata IP refused", "accepted", "raised")
    except sheets.SheetError as e:
        check("metadata IP refused", "non-public" in str(e), True)
finally:
    sheets.socket.getaddrinfo = real_getaddrinfo


# --------------------------------------------------------------------------- #
print("\n4. fetch behaviour, with a fake network")


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, ctype="text/csv"):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def with_opener(fn):
    real = sheets._open_no_redirect
    sheets._open_no_redirect = fn
    real_ip = sheets._assert_public_ip
    sheets._assert_public_ip = lambda host: None      # DNS is not under test here
    try:
        return None
    finally:
        pass


def run_fetch(opener, url=None):
    real_open, real_ip = sheets._open_no_redirect, sheets._assert_public_ip
    sheets._open_no_redirect = opener
    sheets._assert_public_ip = lambda host: None
    try:
        return sheets.fetch_csv(url or
                                f"https://docs.google.com/spreadsheets/d/{DOC}/edit")
    finally:
        sheets._open_no_redirect, sheets._assert_public_ip = real_open, real_ip


CSV = b"Account,Link\nAlice,https://x.com/alice/status/1\nBob,https://x.com/bob/status/2\n"
check("a good CSV comes back", run_fetch(lambda u: FakeResponse(CSV)).count("\n"), 3)

print("   ...and it parses into rows through the same path as an upload")
grid = uploads.grid_from_csv_text(run_fetch(lambda u: FakeResponse(CSV)))
report = uploads.analyse(grid)
check("2 rows", len(report["rows"]), 2)
check("account column honoured", report["rows"][0]["account_name"], "Alice")


def expect_error(name, opener, needle):
    try:
        run_fetch(opener)
    except sheets.SheetError as e:
        ok = needle.lower() in str(e).lower()
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {str(e)[:70]}")
        if not ok:
            FAILS.append(name)
        return
    print(f"  FAIL  {name}: no error raised")
    FAILS.append(name)


expect_error("HTML sign-in page is NOT parsed as CSV",
             lambda u: FakeResponse(b"<html><body>Sign in</body></html>", "text/html"),
             "sign-in page")
expect_error("empty sheet", lambda u: FakeResponse(b"   \n"), "empty")
expect_error("oversize body is cut off",
             lambda u: FakeResponse(b"x" * (sheets.MAX_BYTES + 10)), "larger than")
expect_error("unexpected content type",
             lambda u: FakeResponse(b"%PDF-1.4", "application/pdf"), "expected csv")


def http_error(code):
    def _open(u):
        raise urllib.error.HTTPError(u, code, "boom", {}, None)
    return _open


expect_error("403 explains sharing", http_error(403), "not shared publicly")
expect_error("404 explains the link", http_error(404), "no sheet found")
expect_error("500 is reported honestly", http_error(500), "http 500")

print("\n5. redirects are followed but RE-VALIDATED at every hop")
hops = []


def redirect_to(target):
    def _open(u):
        hops.append(u)
        if len(hops) == 1:
            raise urllib.error.HTTPError(
                u, 302, "moved", {"Location": target}, None)
        return FakeResponse(CSV)
    return _open


hops.clear()
ok_target = "https://doc-00-sheets.googleusercontent.com/export/abc"
check("redirect to googleusercontent is followed",
      run_fetch(redirect_to(ok_target)).startswith("Account"), True)
check("second hop went to the redirect target", hops[1], ok_target)

hops.clear()
expect_error("redirect OFF the allow-list is refused",
             redirect_to("https://evil.example.com/steal"), "only google")
hops.clear()
expect_error("redirect to a private address is refused",
             redirect_to("https://127.0.0.1/admin"), "only google")

print("\n6. no new production dependency")
src = Path(ROOT / "webapp" / "sheets.py").read_text()
for banned in ("import requests", "import httpx", "import aiohttp"):
    check(f"{banned!r} not used", banned in src, False)
check("uses stdlib urllib", "import urllib.request" in src, True)
reqs = (ROOT / "requirements.txt").read_text().lower()
check("requirements.txt untouched by this feature",
      any(p in reqs for p in ("requests", "httpx", "aiohttp")), False)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("SHEETS OK — allow-list holds, redirects revalidated, no new dependency")
