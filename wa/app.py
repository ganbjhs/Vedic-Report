#!/usr/bin/env python3
"""
app.py — desktop UI for wa_toolkit (pywebview: native window + HTML UI, Python backend).

    pip install pywebview
    python app.py

All automation runs in ONE background worker thread that owns the Playwright
session (Playwright objects must stay on the thread that created them). The UI
talks to it through a job queue; logs stream back to the window.
"""
from __future__ import annotations

import io
import json
import queue
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path

try:                      # optional — only the desktop app needs it.
    import webview        # server.py imports this module without pywebview installed.
except Exception:         # noqa: BLE001
    webview = None

import os
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wa.paths import APP_DIR, DATA_DIR, CONFIG, TASKS, UI_INDEX, FROZEN   # noqa: E402
BASE = DATA_DIR
os.chdir(BASE)  # relative paths in config/tasks (file1.txt, plugins/, wa_profile) resolve from here

from wa.session import WASession, SEL          # noqa: E402
from wa.engine import Engine                    # noqa: E402
from wa.chat import open_chat, send_text        # noqa: E402
from wa.reader import read_history              # noqa: E402
from wa.engine import links_from_messages, sender_matches  # noqa: E402
from wa.metrics import fetch_metrics            # noqa: E402



class _LogStream(io.TextIOBase):
    """Captures print() from wa.* modules into the log buffer while a job runs."""
    def __init__(self, sink): self.sink = sink
    def write(self, s):
        if s and s.strip():
            self.sink(s.rstrip("\n"))
        return len(s)


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.jobs: queue.Queue = queue.Queue()
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.status = "idle"          # idle | running | error
        self.current = ""
        self.session: WASession | None = None
        self.engine: Engine | None = None
        self.result = None
        self.keep_browser = True
        self.last_rows: list[dict] = []
        self.chat_name = ""
        self.stop_flag = False

    # -- logging ---------------------------------------------------------
    def emit(self, line: str, level: str = "info"):
        with self.lock:
            self.log.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": str(line)})
            if len(self.log) > 5000:
                del self.log[:1000]

    def log_since(self, n: int):
        with self.lock:
            return self.log[n:], len(self.log)

    # -- session ---------------------------------------------------------
    def _session(self) -> WASession:
        cfg = load_json(CONFIG)
        if getattr(Api, "_bot", None) is not None and Api._bot.poll() is None:
            raise RuntimeError("The bot is running and owns the WhatsApp session. Stop the bot first (Bot tab).")
        if self.session is None:
            self.emit("Launching WhatsApp Web browser…")
            self.session = WASession(cfg.get("profile_dir", "./wa_profile"), headless=cfg.get("headless", False))
            self.session.__enter__()
            self.session.wait_logged_in(timeout_s=300)
            self.emit("Logged in.", "ok")
        else:
            try:  # browser may have been closed by the user
                _ = self.session.page.title()
            except Exception:  # noqa: BLE001
                self.emit("Browser was closed — relaunching…", "warn")
                self.session = None
                return self._session()
        return self.session

    def close_browser(self):
        if self.session:
            try:
                self.session.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.session = None
            self.emit("Browser closed.")

    # -- main loop -------------------------------------------------------
    def run(self):
        while True:
            job = self.jobs.get()
            kind, payload = job["kind"], job.get("payload", {})
            if job.get("silent"):
                try:
                    getattr(self, f"job_{kind}")(**payload)
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.status, self.current, self.result = "running", kind, None
            self.stop_flag = False
            try:
                with redirect_stdout(_LogStream(self.emit)):
                    handler = getattr(self, f"job_{kind}")
                    self.result = handler(**payload)
                self.status = "idle"
                self.emit(f"✔ {kind} finished", "ok")
            except KeyboardInterrupt:
                self.status = "idle"
                self.emit("■ stopped by user", "warn")
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.emit(f"✖ {kind} failed: {type(e).__name__}: {e}", "error")
                self.emit(" | ".join(traceback.format_exc().strip().splitlines()[-3:]), "error")
            finally:
                self.current = ""
                self.engine = None
                if not self.keep_browser:
                    self.close_browser()

    # -- jobs ------------------------------------------------------------
    def job_login(self):
        self.close_browser()
        self._session()
        return "ok"

    def job_run_task(self, name: str, vars: dict | None = None):
        tasks = load_json(TASKS)
        if name not in tasks:
            raise ValueError(f"task '{name}' not found")
        t = tasks[name]
        s = self._session()
        cfg = load_json(CONFIG)
        self.engine = Engine(s, cfg, log_fn=self.emit)
        self.engine.ctx.update(t.get("vars", {}))
        self.engine.ctx.update(vars or {})
        self.emit(f"▶ running task '{name}' vars={ {k: self.engine.ctx[k] for k in t.get('vars', {})} }")
        self.engine.run(t["steps"])
        return "ok"

    def job_read_chat(self, chat: str, n: int = 50, days: int | None = None):
        s = self._session()
        open_chat(s, chat)
        msgs = read_history(s, max_messages=int(n), days=int(days) if days else None)
        rows = []
        for m in msgs:
            rows.append({"time": m["time"].strftime("%Y-%m-%d %H:%M") if m["time"] else "",
                         "sender": m["sender"], "phone": m["phone"], "text": m["text"][:300],
                         "links": m["links"], "outgoing": m["outgoing"]})
        senders = sorted({(m["sender"], m["phone"]) for m in msgs if m["sender"]})
        self.emit(f"read {len(rows)} messages from '{chat}'")
        return {"messages": rows, "senders": [{"sender": a, "phone": b} for a, b in senders]}

    def job_debug(self, chat: str | None = None):
        s = self._session()
        if chat:
            open_chat(s, chat)
        out = {}
        for key, alts in SEL.items():
            hits = [(css, s.page.locator(css).count()) for css in alts]
            out[key] = [{"css": c, "count": n} for c, n in hits]
        editors = s.dump_editors()
        s.screenshot(str(BASE / "debug.png"))
        return {"selectors": out, "editors": editors, "screenshot": "debug.png"}

    def job_close_browser(self):
        self.close_browser()
        return "ok"

    # ---- simple mode jobs (act on the chat currently open in the WhatsApp window)
    def job_current_chat(self, quiet: bool = True):
        if self.session is None:
            return ""
        self.chat_name = self.session.current_chat()
        return self.chat_name

    def _need_chat(self) -> str:
        s = self._session()
        name = s.current_chat()
        if not name:
            raise RuntimeError("No chat is open in the WhatsApp window — click a chat or group there first.")
        self.chat_name = name
        return name

    def job_scan(self, days: int | None = 7, n: int = 400):
        name = self._need_chat()
        s = self.session
        self.emit(f"Scanning '{name}' (last {days} days, up to {n} messages)…")
        msgs = read_history(s, max_messages=int(n), days=int(days) if days else None)
        counts: dict[tuple, dict] = {}
        for m in msgs:
            if m["outgoing"]:
                continue
            k = (m["sender"], m["phone"])
            c = counts.setdefault(k, {"sender": m["sender"], "phone": m["phone"], "messages": 0, "links": 0})
            c["messages"] += 1
            c["links"] += len(m["links"])
        senders = sorted(counts.values(), key=lambda x: -x["links"])
        self.emit(f"{len(msgs)} messages, {sum(c['links'] for c in senders)} links, {len(senders)} senders")
        return {"chat": name, "messages": len(msgs), "senders": senders}

    def job_send_lines(self, lines: list[str], text: str = "", delay: float = 1.5):
        name = self._need_chat()
        s = self.session
        lines = [l.strip() for l in lines if l and l.strip()]
        self.emit(f"Sending {len(lines)} messages to '{name}'…")
        for i, line in enumerate(lines, 1):
            if self.stop_flag:
                raise KeyboardInterrupt
            send_text(s, f"{line}\n{text}" if text else line, delay_after=float(delay))
            self.emit(f"  {i}/{len(lines)} sent")
        return {"sent": len(lines), "chat": name}

    def job_collect(self, days: int | None = 7, n: int = 400, senders: list | None = None,
                    kinds: list | None = None, metrics: bool = False, groups: list | None = None,
                    include_me: bool = False):
        s = self._session()
        targets = groups or [self._need_chat()]
        rows: list[dict] = []
        seen: set[str] = set()
        for g in targets:
            if groups:
                self.emit(f"Opening '{g}'…")
                open_chat(s, g)
            gname = s.current_chat() or g
            msgs = read_history(s, max_messages=int(n), days=int(days) if days else None)
            kept = [m for m in msgs if (include_me or not m["outgoing"]) and sender_matches(m, senders or [])]
            found = links_from_messages(kept, gname, kinds or ["post", "comment"], None, True)
            new = [r for r in found if r["clean_url"] not in seen]
            seen.update(r["clean_url"] for r in new)
            rows.extend(new)
            self.emit(f"'{gname}': {len(msgs)} messages → {len(kept)} from selected senders → {len(new)} links")
        if metrics and rows:
            page = s.new_tab()
            for i, r in enumerate(rows, 1):
                r.update(fetch_metrics(r["platform"], r["url"], page))
                self.emit(f"  metrics {i}/{len(rows)} {r['platform']} likes={r['likes']} comments={r['comments']}")
                time.sleep(3)
            page.close()
        self.last_rows = rows
        return {"rows": rows, "count": len(rows)}

    def job_save(self, mode: str = "csv", rows: list | None = None):
        rows = rows if rows is not None else self.last_rows
        if not rows:
            return {"saved": 0, "where": ""}
        cfg = load_json(CONFIG)
        sc = cfg.get("sheets", {})
        if mode == "sheet":
            from wa.sheets import SheetWriter
            w = SheetWriter(sc["sheet_id"], sc.get("worksheet", "links"), sc.get("creds", "service_account.json"))
            where = f"Google Sheet '{sc.get('worksheet', 'links')}'"
        else:
            from wa.sheets import CsvWriter
            path = BASE / sc.get("csv_path", "links.csv")
            w = CsvWriter(str(path))
            where = str(path)
        have = w.existing_urls()
        new = []
        for r in rows:
            if r["clean_url"] in have:
                continue
            have.add(r["clean_url"])
            new.append(r)
        n = w.append(new)
        self.emit(f"Saved {n} new rows to {where} ({len(rows) - n} were already there)")
        return {"saved": n, "skipped": len(rows) - n, "where": where}


# ------------------------------------------------------------------ helpers
def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_json(p: Path, data: dict):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ JS API
class Api:
    def __init__(self, worker: Worker):
        self.w = worker

    # state
    def status(self):
        return {"status": self.w.status, "current": self.w.current, "queue": self.w.jobs.qsize(),
                "browser": self.w.session is not None, "keep_browser": self.w.keep_browser}

    def log_since(self, n):
        lines, total = self.w.log_since(int(n or 0))
        return {"lines": lines, "total": total}

    def result(self):
        return self.w.result

    # tasks
    def list_tasks(self):
        tasks = load_json(TASKS)
        return [{"name": k, "description": v.get("description", ""), "vars": v.get("vars", {}),
                 "steps": len(v.get("steps", []))} for k, v in tasks.items() if not k.startswith("_")]

    def get_task(self, name):
        return load_json(TASKS).get(name)

    def save_task(self, name, task_json):
        tasks = load_json(TASKS)
        task = json.loads(task_json) if isinstance(task_json, str) else task_json
        if "steps" not in task or not isinstance(task["steps"], list):
            raise ValueError("task must have a 'steps' list")
        tasks[name] = task
        save_json(TASKS, tasks)
        return "ok"

    def delete_task(self, name):
        tasks = load_json(TASKS)
        tasks.pop(name, None)
        save_json(TASKS, tasks)
        return "ok"

    def actions_help(self):
        from wa.engine import HELP
        return HELP

    # config
    def get_config(self):
        return load_json(CONFIG)

    def save_config(self, cfg_json):
        cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else cfg_json
        save_json(CONFIG, cfg)
        return "ok"

    def set_keep_browser(self, v):
        self.w.keep_browser = bool(v)
        return self.w.keep_browser

    # jobs
    def run_task(self, name, vars=None):
        if isinstance(vars, str):
            vars = json.loads(vars or "{}")
        self.w.jobs.put({"kind": "run_task", "payload": {"name": name, "vars": vars or {}}})
        return "queued"

    def stop(self):
        if self.w.engine:
            self.w.engine.stop_requested = True
        self.w.stop_flag = True
        # also drain the queue
        try:
            while True:
                self.w.jobs.get_nowait()
        except queue.Empty:
            pass
        return "stopping"

    def login(self):
        self.w.jobs.put({"kind": "login"}); return "queued"

    def read_chat(self, chat, n=50, days=None):
        self.w.jobs.put({"kind": "read_chat", "payload": {"chat": chat, "n": n, "days": days}}); return "queued"

    def debug(self, chat=None):
        self.w.jobs.put({"kind": "debug", "payload": {"chat": chat or None}}); return "queued"

    def close_browser(self):
        self.w.jobs.put({"kind": "close_browser"}); return "queued"

    # ---- simple mode
    def current_chat(self):
        # cheap read done on the worker thread only when idle (Playwright is thread-bound)
        if self.w.status == "running" or self.w.session is None:
            return {"chat": self.w.chat_name, "browser": self.w.session is not None}
        self.w.jobs.put({"kind": "current_chat", "payload": {}, "silent": True})
        return {"chat": self.w.chat_name, "browser": True}

    def scan(self, days=7, n=400):
        self.w.jobs.put({"kind": "scan", "payload": {"days": days, "n": n}}); return "queued"

    def send_lines(self, lines, text="", delay=1.5):
        if isinstance(lines, str):
            lines = lines.splitlines()
        self.w.jobs.put({"kind": "send_lines", "payload": {"lines": lines, "text": text, "delay": delay}}); return "queued"

    def collect(self, opts):
        if isinstance(opts, str):
            opts = json.loads(opts)
        self.w.jobs.put({"kind": "collect", "payload": opts}); return "queued"

    def save_rows(self, mode="csv"):
        self.w.jobs.put({"kind": "save", "payload": {"mode": mode}}); return "queued"

    def last_rows(self):
        return self.w.last_rows

    def pick_file(self):
        try:
            win = webview.windows[0]
            res = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                         file_types=("Text files (*.txt;*.csv)", "All files (*.*)"))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        return {"path": str(path), "content": content}

    def groups(self):
        return load_json(CONFIG).get("collect", {}).get("groups", [])

    def set_groups(self, groups):
        cfg = load_json(CONFIG)
        cfg.setdefault("collect", {})["groups"] = list(groups or [])
        save_json(CONFIG, cfg)
        return cfg["collect"]["groups"]

    def sheets_ready(self):
        sc = load_json(CONFIG).get("sheets", {})
        ok = bool(sc.get("sheet_id")) and "PASTE" not in sc.get("sheet_id", "") and \
            (BASE / sc.get("creds", "service_account.json")).exists()
        return {"ready": ok, "csv_path": str(BASE / sc.get("csv_path", "links.csv"))}

    # ---- command bot (runs as a separate quiet process so it never blocks the app)
    _bot = None
    _bot_lines: list[str] = []

    def bot_status(self):
        alive = self._bot is not None and self._bot.poll() is None
        return {"running": alive, "lines": self._bot_lines[-30:], "group": load_json(CONFIG).get("bot", {}).get("group", "")}

    def bot_start(self, group, headed=False):
        import subprocess, threading as _th
        if self._bot is not None and self._bot.poll() is None:
            return {"running": True}
        cfg = load_json(CONFIG)
        cfg.setdefault("bot", {})["group"] = group
        save_json(CONFIG, cfg)
        # one Chromium profile = one browser at a time -> close the app's window first
        if self.w.session is not None:
            self.w.jobs.put({"kind": "close_browser", "payload": {}, "silent": True})
            for _ in range(100):
                if self.w.session is None:
                    break
                time.sleep(0.1)
        if FROZEN:   # same executable, bot mode
            cmd = [sys.executable, "--bot", "--group", group] + (["--headed"] if headed else [])
        else:
            cmd = [sys.executable, str(APP_DIR / "bot.py"), "--group", group] + (["--headed"] if headed else [])
        self._bot_lines.clear()
        self._bot = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        def pump(proc=self._bot):
            for line in proc.stdout:
                self._bot_lines.append(line.rstrip())
                if len(self._bot_lines) > 500:
                    del self._bot_lines[:100]
        _th.Thread(target=pump, daemon=True).start()
        return {"running": True}

    def bot_stop(self):
        if self._bot is not None and self._bot.poll() is None:
            self._bot.terminate()
            try:
                self._bot.wait(5)
            except Exception:  # noqa: BLE001
                self._bot.kill()
        self._bot = None
        return {"running": False}

    def open_folder(self):
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(BASE)])
        elif sys.platform == "win32":
            os.startfile(str(BASE))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(BASE)])
        return "ok"


def main():
    if webview is None:
        raise SystemExit("pywebview is not installed — this is the desktop app. "
                         "For the server build run: uvicorn server:app")
    worker = Worker()
    worker.start()
    api = Api(worker)
    webview.create_window("WA Toolkit", str(UI_INDEX), js_api=api,
                          width=1180, height=780, min_size=(900, 600))
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    if "--bot" in sys.argv:            # frozen build runs the bot through the same executable
        sys.argv.remove("--bot")
        import bot
        bot.main()
    else:
        main()
