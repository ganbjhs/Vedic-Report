"""Client Portal — the client-facing web app (see docs/client-portal-plan.md).

Everything a client sees comes from ONE SQLite file, `data/portal.db`, that
the internal tool publishes into. The portal never reads `jobs.db`, never
opens `data/jobs/`, never talks to Google Sheets and never runs a browser.

Package layout (leaf modules first — these are the only ones `webapp/`
may import, and they depend on nothing but the standard library):

    schema.py    the tables + ensure_schema()
    util.py      url normalisation, number parsing, platform/category guessing
    secretbox.py encrypt/decrypt the scraper API key with PORTAL_KEY_SECRET
    scraper.py   fetch + normalise the scraper's JSON (FIELD_MAP lives here)

    config.py    environment
    db.py        connections, the visibility rule, audit log
    auth.py      invites, login, server-side sessions, rate limit
    queries.py   meta / daily / trend
    export.py    the Excel workbook
    routes.py    pages + JSON API
    main.py      the FastAPI app

Run locally:

    .venv/bin/pip install -r requirements-portal.txt
    PORTAL_DB=data/portal.db .venv/bin/python -m uvicorn portal.main:app --port 8020
"""
