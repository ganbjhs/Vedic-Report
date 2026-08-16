"""Per-platform session health and the daily capture budget.

WHY THIS FILE EXISTS. The dashboard used to answer "is the X login OK?" twice,
two different ways:

  * the submit page passed `config.X_STATE_FILE.exists()` — a file test
  * the admin page passed `x_login.session_is_valid()` — a cookie test

Those disagree exactly when it matters. A cookie that is present but expired
(or six hours from expiring, which is the same thing for a long run) makes the
file test say *fine* and the real check say *no*. The banner stayed silent and
the report came back full of login walls.

One function now answers it, for every platform, in the shape the pills render
from. `x_login.session_is_valid()` remains the single underlying truth — this
module classifies it, it does not re-derive it.
"""
import datetime

from . import config, report_types, x_login
from .jobs import store

# A session with less than this left will not survive a long run, so it is
# reported as expiring rather than healthy — the point is to re-auth BEFORE
# spending browser minutes, not after.
RENEW_WITHIN_HOURS = 24

# States, worst first. `state` is what the pill colours on; never parse `text`.
OK, EXPIRING, INVALID, MISSING, NO_ENGINE = (
    "ok", "expiring", "invalid", "missing", "no-engine")


def _x_health() -> dict:
    """Classify the one platform that has an engine."""
    info = x_login.session_state()
    auto = "The server can sign in again by itself." if info["auto_login"] else (
        "No X_USERNAME / X_PASSWORD, so the server cannot renew this itself.")

    if not info["present"]:
        return {"state": MISSING,
                "text": "No X login saved on the server.", "detail": auto}

    # session_is_valid() is THE check — the same one a capture will make.
    if not info["valid"]:
        return {"state": INVALID,
                "text": "Saved X login has expired.", "detail": auto}

    days = info.get("expires_in_days")
    if days is not None and days * 24 <= RENEW_WITHIN_HOURS:
        return {"state": EXPIRING,
                "text": "X login expires within a day.", "detail": auto}

    when = (f"Expires in {days} days." if days is not None
            else "No expiry on the cookie.")
    return {"state": OK, "text": "X login is valid.", "detail": when}


def _fb_health() -> dict:
    """Facebook captures public posts logged-out, so 'no session' is the normal,
    healthy state — not a warning. A saved sessions/fb_state.json is reported
    when present."""
    f = config.SESSIONS_DIR / "fb_state.json"
    if f.exists():
        return {"state": OK, "text": "Facebook session saved.",
                "detail": "Captures run signed in with it."}
    return {"state": OK, "text": "Public posts, no account needed.",
            "detail": ("Captures run logged-out. To capture with an account, "
                       "save a Playwright storage state at "
                       f"{f} (same shape as x_state.json).")}


def platform_health() -> list:
    """One entry per platform, in `PLATFORMS` order, ready to render as pills.

    A platform with no capture engine reports NO_ENGINE rather than a fake
    green: nothing is wrong with it, but nothing is signed in either, and
    showing it as healthy would promise a report that cannot be made.
    """
    out = []
    for p in report_types.PLATFORMS:
        if p.combines:
            continue                    # a mode, not an account — no session
        if not p.live:
            health = {"state": NO_ENGINE, "text": f"{p.label} — not available yet",
                      "detail": p.note}
        elif p.slug == "x":
            health = _x_health()
        elif p.slug == "facebook":
            health = _fb_health()
        else:                           # pragma: no cover - no such platform yet
            # A platform marked live with no health check is a bug in the table,
            # and saying so beats defaulting to green (rule 17).
            health = {"state": INVALID,
                      "text": f"{p.label} has no health check.",
                      "detail": "Add one to webapp/health.py when its engine lands."}
        out.append({"slug": p.slug, "label": p.label, "live": p.live,
                    "badge": p.badge, **health})
    return out


def capture_budget() -> dict:
    """Today's capture count against the rule-21 ceiling.

    `over` is possible and is not clamped — going past the ceiling is exactly
    the thing worth showing, and a meter pinned at 100% would hide it.
    """
    used = store.captures_today()
    total = config.DAILY_CAPTURE_BUDGET
    pct = round(used / total * 100) if total else 0
    return {
        "used": used,
        "total": total,
        "left": max(0, total - used),
        "percent": min(100, pct),
        "over": used > total,
        # Warn before the ceiling, not at it: quality starts sliding as the
        # account tires, so 80% is the point to think about splitting a run.
        "warn": used >= total * 0.8,
        "resets_text": "Resets at midnight",
        "as_of": datetime.datetime.now().strftime("%H:%M"),
    }
