"""
Google Sheets output via gspread + a service account (no server needed).

One-time setup:
  1. https://console.cloud.google.com → new project → enable "Google Sheets API"
     and "Google Drive API".
  2. IAM & Admin → Service Accounts → create → Keys → "Add key" (JSON) →
     save it as  service_account.json  next to wa.py.
  3. Open your Google Sheet → Share → add the service-account e-mail
     (…@…iam.gserviceaccount.com) as Editor.
  4. Put the sheet ID (the long part of the URL) in config.json.
"""
from __future__ import annotations

import csv
from pathlib import Path

HEADERS = ["collected_at", "group", "sender", "phone", "sent_at", "platform", "kind",
           "url", "clean_url", "message", "likes", "comments", "shares", "views", "metrics_note"]


class SheetWriter:
    def __init__(self, sheet_id: str, worksheet: str = "links", creds: str = "service_account.json"):
        import gspread  # imported lazily so the rest works without it
        self.gc = gspread.service_account(filename=creds)
        self.sh = self.gc.open_by_key(sheet_id)
        try:
            self.ws = self.sh.worksheet(worksheet)
        except gspread.WorksheetNotFound:
            self.ws = self.sh.add_worksheet(title=worksheet, rows=1000, cols=len(HEADERS))
        first = self.ws.row_values(1)
        if first != HEADERS:
            if not first:
                self.ws.append_row(HEADERS)
            else:  # keep the user's own header but make sure ours exist
                missing = [h for h in HEADERS if h not in first]
                if missing:
                    self.ws.update_cell(1, len(first) + 1, missing[0])
                    for i, h in enumerate(missing[1:], start=len(first) + 2):
                        self.ws.update_cell(1, i, h)
        self.header = self.ws.row_values(1)

    def existing_urls(self) -> set[str]:
        if "clean_url" not in self.header:
            return set()
        col = self.header.index("clean_url") + 1
        return set(v for v in self.ws.col_values(col)[1:] if v)

    def append(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        values = [[str(r.get(h, "") if r.get(h, "") is not None else "") for h in self.header] for r in rows]
        self.ws.append_rows(values, value_input_option="USER_ENTERED")
        return len(values)


class CsvWriter:
    """Fallback: local CSV you can import into Sheets manually."""

    def __init__(self, path: str = "links.csv"):
        self.path = Path(path)
        self.header = HEADERS
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(HEADERS)

    def existing_urls(self) -> set[str]:
        with self.path.open(newline="", encoding="utf-8") as f:
            return {r.get("clean_url", "") for r in csv.DictReader(f) if r.get("clean_url")}

    def append(self, rows: list[dict]) -> int:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
            for r in rows:
                w.writerow({h: r.get(h, "") for h in HEADERS})
        return len(rows)
