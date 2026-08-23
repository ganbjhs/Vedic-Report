"""
Example plugin. Any function here can be called from tasks.json with
  {"action": "python", "func": "example.<function>", "args": {...}, "save_as": "..."}
Signature: fn(session, ctx, **args) -> value (stored under save_as if given).

`session` is wa.session.WASession (session.page = Playwright page of WhatsApp Web),
`ctx` is the task's variable dict. You can import anything from wa.* here.
"""
from collections import Counter


def summarise(session, ctx, msgs=None, **_):
    msgs = msgs or []
    by_sender = Counter(m["sender"] for m in msgs if m.get("sender"))
    with_links = sum(1 for m in msgs if m.get("links"))
    lines = [f"{len(msgs)} messages, {with_links} with links"] + \
            [f"  {n:20} {c}" for n, c in by_sender.most_common(10)]
    return "\n".join(lines)


def send_to_each_sender(session, ctx, msgs=None, text="thanks!", **_):
    """Example of driving the browser yourself: DM every distinct sender."""
    from wa.chat import open_chat, send_text
    for sender in sorted({m["sender"] for m in (msgs or []) if m.get("sender") and not m.get("outgoing")}):
        open_chat(session, sender)
        send_text(session, text)
