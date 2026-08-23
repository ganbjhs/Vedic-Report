import React, { useEffect, useState, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// ---------------------------------------------------------------- api bridge
// Server build: the Python side is served over HTTP by server.py instead of
// being injected into a pywebview window. Same method names, same arguments.
// The base is taken from the page's own URL so the app does not care which
// prefix it is mounted under (/wa/ in production, / when run bare).
const API_BASE = (() => {
  const p = new URL(".", window.location.href).pathname;
  return p.endsWith("/") ? p : p + "/";
})();

async function api(name, ...args) {
  const r = await fetch(API_BASE + "api/" + name, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-WA-Api": "1" },
    body: JSON.stringify({ args }),
  });
  if (r.status === 401) {                    // session expired -> Report Maker login
    window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
    return null;
  }
  const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
  if (!r.ok || j.ok === false) throw new Error(j.error || r.statusText);
  return j.result;
}

async function uploadFile(f) {
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch(API_BASE + "upload", { method: "POST", credentials: "same-origin", body: fd });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
let toastFn = () => {};
const toast = (m) => toastFn(m);
async function safe(name, ...args) {
  try { return await api(name, ...args); }
  catch (e) { toast("Error: " + (e?.message || e)); throw e; }
}

// ---------------------------------------------------------------- root
function App() {
  const [page, setPage] = useState("send");
  const [st, setSt] = useState({ status: "idle", current: "", browser: false });
  const [chat, setChat] = useState("");
  const [bot, setBot] = useState({ running: false, lines: [], group: "" });
  const [log, setLog] = useState([]);
  const [msg, setMsg] = useState(null);
  const logN = useRef(0);
  const [done, setDone] = useState(0);         // increments when a job finishes
  const lastStatus = useRef("idle");
  toastFn = (m) => { setMsg(m); clearTimeout(toastFn._h); toastFn._h = setTimeout(() => setMsg(null), 3200); };

  useEffect(() => {
    let alive = true, n = 0;
    const tick = async () => {
      try {
        const s = await api("status"); setSt(s);
        const c = await api("current_chat"); setChat(c.chat || "");
        const l = await api("log_since", logN.current);
        if (l.lines.length) { logN.current = l.total; setLog((p) => [...p, ...l.lines].slice(-800)); }
        if (n++ % 3 === 0) setBot(await api("bot_status"));
        if (lastStatus.current === "running" && s.status !== "running") setDone((d) => d + 1);
        lastStatus.current = s.status;
      } catch (e) { /* bridge not ready yet */ }
      if (alive) setTimeout(tick, 900);
    };
    tick();
    return () => { alive = false; };
  }, []);

  const busy = st.status === "running";
  const pages = { send: SendPage, collect: CollectPage, bot: BotPage, adv: AdvancedPage };
  const Page = pages[page];
  return (
    <div className="app">
      <aside className="side">
        <div className="brand"><span className="logo">W</span>WA Toolkit</div>
        <div className="status">
          <span className={"dot " + (bot.running ? "ok" : busy ? "busy" : st.browser ? "ok" : "")} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {bot.running ? "Bot running" : busy ? `Working: ${st.current}` : st.browser ? (chat ? chat : "WhatsApp open") : "WhatsApp closed"}
          </span>
        </div>
        {[["send", "✉️", "Send messages"], ["collect", "🔗", "Collect links"], ["bot", "🤖", "Bot (from phone)"], ["adv", "⚙️", "Advanced"]].map(([k, ic, t]) => (
          <button key={k} className={"navbtn " + (page === k ? "on" : "")} onClick={() => setPage(k)}><span className="ic">{ic}</span>{t}</button>
        ))}
        <div className="foot">
          <div className="row" style={{ gap: 6 }}>
            {!st.browser && !bot.running && <button className="btn sm" onClick={() => safe("login")}>Open WhatsApp</button>}
            {st.browser && <button className="btn sm" onClick={() => safe("close_browser")}>Close WhatsApp</button>}
            {busy && <button className="btn sm d" onClick={() => safe("stop")}>Stop</button>}
          </div>
        </div>
      </aside>
      <main className="main">
        <Page chat={chat} st={st} bot={bot} done={done} busy={busy} />
      </main>
      <LogDrawer log={log} onClear={() => setLog([])} />
      <QrPanel />
      {msg && <div className="toast">{msg}</div>}
    </div>
  );
}

// The desktop app opened a Chromium window and you scanned the QR off your own
// screen. On a server there is no screen, so server.py screenshots the headless
// login page every 2s and this shows it until WhatsApp reports us signed in.
function QrPanel() {
  const [state, setState] = useState("idle");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const q = await api("qr_state");
        if (!alive || !q) return;
        setState(q.state);
        if (q.state === "waiting") setTick((n) => n + 1);
      } catch (e) { /* signed out or server restarting */ }
      if (alive) setTimeout(poll, 2000);
    };
    poll();
    return () => { alive = false; };
  }, []);
  if (state !== "waiting") return null;
  return (
    <div className="qr-overlay">
      <div className="qr-box">
        <h2>Scan to link WhatsApp</h2>
        <p className="sub">WhatsApp on your phone → Settings → Linked devices → Link a device.</p>
        <img src={API_BASE + "qr.png?t=" + tick} alt="WhatsApp QR code" />
        <div className="row">
          <button className="btn" onClick={() => safe("stop")}>Cancel</button>
          <span className="hint">This closes by itself once the phone confirms.</span>
        </div>
      </div>
    </div>
  );
}

function LogDrawer({ log, onClear }) {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [log, open]);
  const last = log[log.length - 1];
  return (
    <div className={"drawer " + (open ? "open" : "")}>
      <div className="bar">
        <span>Activity</span>
        {!open && last && <span className={"l-" + last.level} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{last.msg}</span>}
        <span className="grow" />
        <button className="btn sm" onClick={onClear}>clear</button>
        <button className="btn sm" onClick={() => setOpen(!open)}>{open ? "hide" : "show"}</button>
      </div>
      <pre ref={ref}>{log.map((x, i) => <div key={i}><span className="t">{x.t}</span><span className={"l-" + x.level}>{x.msg}</span></div>)}</pre>
    </div>
  );
}

// ---------------------------------------------------------------- Send
function SendPage({ chat, busy }) {
  const [lines, setLines] = useState("");
  const [extra, setExtra] = useState("");
  const [delay, setDelay] = useState(1.5);
  const [file, setFile] = useState("");
  const count = lines.split("\n").filter((x) => x.trim()).length;
  const fileRef = useRef();
  const pick = () => fileRef.current?.click();
  const onFile = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";                       // same file twice still fires
    if (!f) return;
    try { const r = await uploadFile(f); setLines(r.content); setFile(r.path.split(/[\\/]/).pop()); }
    catch (err) { toast("Upload failed: " + (err?.message || err)); }
  };
  const send = async () => {
    if (!count) return toast("Nothing to send");
    if (!confirm(`Send ${count} message(s) to “${chat}”?`)) return;
    await safe("send_lines", lines.split("\n"), extra, +delay || 1.5); toast("Sending…");
  };
  return (
    <div className="page">
      <div><h1>Send messages</h1><p className="sub">Messages go to whichever chat is open in the WhatsApp window.</p></div>
      <div className={"banner " + (chat ? "" : "warn")}>{chat ? <>Target: <b>{chat}</b></> : "Open a chat or group in the WhatsApp window (Open WhatsApp on the left)."}</div>
      <div className="card">
        <label className="f">Messages — one per line
          <textarea rows={9} value={lines} onChange={(e) => setLines(e.target.value)} placeholder="Paste your messages here, one message per line…" />
        </label>
        <div className="row"><input ref={fileRef} type="file" accept=".txt,.csv,text/plain" style={{ display: "none" }} onChange={onFile} /><button className="btn" onClick={pick}>Load from .txt file</button><span className="hint">{file}</span><span className="right hint">{count} message(s)</span></div>
        <label className="f">Add under every message (optional)<input value={extra} onChange={(e) => setExtra(e.target.value)} placeholder="e.g. a link" /></label>
        <div className="row">
          <label className="f" style={{ width: 170 }}>Seconds between messages<input type="number" step="0.5" min="0.5" value={delay} onChange={(e) => setDelay(e.target.value)} /></label>
          <button className="btn p lg right" disabled={!chat || busy} onClick={send}>Send to {chat ? `“${chat.slice(0, 26)}”` : "chat"}</button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Collect
function CollectPage({ chat, done, busy }) {
  const [groups, setGroups] = useState([]);
  const [useGroups, setUseGroups] = useState(false);
  const [days, setDays] = useState(7); const [max, setMax] = useState(400);
  const [post, setPost] = useState(true); const [comment, setComment] = useState(true); const [metrics, setMetrics] = useState(false);
  const [senders, setSenders] = useState([]); const [senderText, setSenderText] = useState("");
  const [rows, setRows] = useState(null); const [sheets, setSheets] = useState({ ready: false, csv_path: "" });
  const pending = useRef(null);
  useEffect(() => { safe("groups").then(setGroups); safe("sheets_ready").then(setSheets); }, []);
  useEffect(() => {
    if (!pending.current) return;
    const k = pending.current; pending.current = null;
    api("result").then((r) => {
      if (k === "scan" && r?.senders) { setSenders(r.senders.map((s) => ({ ...s, on: false }))); toast(`${r.senders.length} people found`); }
      if (k === "collect" && r?.rows) { setRows(r.rows); toast(`${r.rows.length} links`); }
    });
  }, [done]);
  const saveGroups = async (g) => setGroups(await safe("set_groups", g));
  const addCurrent = () => chat ? (groups.includes(chat) ? toast("already saved") : saveGroups([...groups, chat])) : toast("Open a group first");
  const scan = async () => { if (!chat) return toast("Open a group first"); pending.current = "scan"; await safe("scan", +days, +max); toast("Scanning…"); };
  const collect = async () => {
    if (useGroups && !groups.length) return toast("No saved groups"); if (!useGroups && !chat) return toast("Open a group first");
    const kinds = [post && "post", comment && "comment"].filter(Boolean); if (!kinds.length) return toast("Tick Post and/or Comment");
    const sel = senders.filter((s) => s.on).map((s) => s.phone || s.sender).concat(senderText.split(",").map((x) => x.trim()).filter(Boolean));
    pending.current = "collect";
    await safe("collect", JSON.stringify({ days: +days, n: +max, senders: sel, kinds, metrics, groups: useGroups ? groups : null })); toast("Collecting…");
  };
  const copy = async () => {
    const cols = ["collected_at", "group", "sender", "phone", "sent_at", "platform", "kind", "clean_url", "message", "likes", "comments", "shares", "views"];
    const tsv = [cols.join("\t"), ...rows.map((r) => cols.map((c) => String(r[c] ?? "").replace(/[\t\n]/g, " ")).join("\t"))].join("\n");
    await navigator.clipboard.writeText(tsv); toast("Copied — paste into Google Sheets");
  };
  return (
    <div className="page">
      <div><h1>Collect links</h1><p className="sub">Pull post / comment links out of groups, filtered by who posted them.</p></div>
      <div className="card">
        <div className="row"><div className={"banner grow " + (chat ? "" : "warn")}>{chat ? <>Open group: <b>{chat}</b></> : "Open a group in the WhatsApp window."}</div><button className="btn" onClick={addCurrent}>＋ Save this group</button></div>
        <div className="row"><span className="hint">My groups:</span>
          <div className="chips">{groups.length ? groups.map((g, i) => <span key={g} className="chip">{g}<span className="x" onClick={() => saveGroups(groups.filter((_, j) => j !== i))}>✕</span></span>) : <span className="hint">none saved yet</span>}</div>
          <label className="check right"><input type="checkbox" checked={useGroups} onChange={(e) => setUseGroups(e.target.checked)} />collect from all my saved groups</label></div>
      </div>
      <div className="card">
        <h2>What to collect</h2>
        <div className="row">
          <label className="f" style={{ width: 130 }}>Look back (days)<input type="number" min="1" value={days} onChange={(e) => setDays(e.target.value)} /></label>
          <label className="f" style={{ width: 130 }}>Max messages<input type="number" min="20" value={max} onChange={(e) => setMax(e.target.value)} /></label>
          <label className="check"><input type="checkbox" checked={post} onChange={(e) => setPost(e.target.checked)} />Post links</label>
          <label className="check"><input type="checkbox" checked={comment} onChange={(e) => setComment(e.target.checked)} />Comment links</label>
          <label className="check"><input type="checkbox" checked={metrics} onChange={(e) => setMetrics(e.target.checked)} />Also fetch likes / comments (slow)</label>
        </div>
        <h2>Only from these people <span className="hint" style={{ fontWeight: 400 }}>— none ticked = everyone</span></h2>
        <div className="row"><button className="btn" onClick={scan} disabled={busy}>Scan open group for people</button><input style={{ maxWidth: 360 }} value={senderText} onChange={(e) => setSenderText(e.target.value)} placeholder="or type: Rahul, +91 98765 43210" /></div>
        {senders.length > 0 && <div className="chips">{senders.map((s, i) => <span key={i} className={"chip " + (s.on ? "on" : "")} onClick={() => setSenders(senders.map((x, j) => j === i ? { ...x, on: !x.on } : x))}>{s.sender}{s.phone && s.phone !== s.sender && <small>{s.phone}</small>}<small>{s.links} links</small></span>)}</div>}
        <div className="row"><button className="btn p lg right" onClick={collect} disabled={busy}>Collect links</button></div>
      </div>
      {rows && <div className="card">
        <div className="row"><h2>Results — {rows.length} links</h2><span className="right" />
          <button className="btn" onClick={copy}>Copy for Google Sheets</button>
          <button className="btn" onClick={() => safe("save_rows", "csv").then(() => toast("Saved CSV"))}>Save CSV</button>
          <button className="btn" disabled={!sheets.ready} title={sheets.ready ? "" : "Configure sheets in Advanced → Config"} onClick={() => safe("save_rows", "sheet").then(() => toast("Saved to Google Sheet"))}>Save to Google Sheet</button>
          <button className="btn" onClick={() => safe("open_folder")}>Open folder</button></div>
        {!sheets.ready && <div className="hint">Google Sheet not configured — "Copy for Google Sheets" then paste (⌘V) into your sheet, or Save CSV → {sheets.csv_path}</div>}
        <div className="tbl"><table><thead><tr><th>group</th><th>sender</th><th>platform</th><th>kind</th><th>link</th><th>sent</th>{metrics && <><th>likes</th><th>comments</th></>}</tr></thead>
          <tbody>{rows.map((r, i) => <tr key={i}><td>{r.group}</td><td>{r.sender}</td><td>{r.platform}</td><td>{r.kind}</td><td className="mono">{r.clean_url}</td><td className="mono">{r.sent_at}</td>{metrics && <><td>{r.likes}</td><td>{r.comments}</td></>}</tr>)}</tbody></table></div>
      </div>}
    </div>
  );
}

// ---------------------------------------------------------------- Bot
function BotPage({ bot }) {
  const [group, setGroup] = useState(""); const [headed, setHeaded] = useState(false); const [showLog, setShowLog] = useState(false);
  useEffect(() => { if (bot.group && !group) setGroup(bot.group); }, [bot.group]);
  const start = async () => { if (!group.trim()) return toast("Type the control group name"); await safe("bot_start", group.trim(), headed); toast("Bot starting…"); };
  const stop = async () => { await safe("bot_stop"); toast("Bot stopped"); };
  const err = bot.lines.some((l) => /Error|Traceback/.test(l));
  return (
    <div className="page">
      <div><h1>Bot — control it from your phone</h1><p className="sub">Runs quietly in the background and answers inside a WhatsApp group. No window, no QR again.</p></div>
      <div className="card">
        <div className="row">
          <label className="f grow">Control group (exact name in WhatsApp)<input value={group} onChange={(e) => setGroup(e.target.value)} placeholder="e.g. Bot Control" disabled={bot.running} /></label>
          <label className="check" style={{ alignSelf: "flex-end", paddingBottom: 10 }}><input type="checkbox" checked={headed} onChange={(e) => setHeaded(e.target.checked)} disabled={bot.running} />show browser</label>
          {!bot.running ? <button className="btn p lg" style={{ alignSelf: "flex-end" }} onClick={start}>Start bot</button>
            : <button className="btn d lg" style={{ alignSelf: "flex-end" }} onClick={stop}>Stop bot</button>}
        </div>
        <div className={"banner " + (bot.running ? (err ? "err" : "") : "warn")}>
          {bot.running ? (err ? "Bot hit an error — see details below." : <>Bot is running. Open <b>{group}</b> on your phone and send <code>/start</code>.</>) : "Bot is stopped."}
        </div>
        <div className="row"><button className="btn sm" onClick={() => setShowLog(!showLog)}>{showLog ? "hide" : "show"} details</button>
          {bot.lines.length > 0 && <button className="btn sm" onClick={() => { navigator.clipboard.writeText(bot.lines.join("\n")); toast("copied"); }}>copy details</button>}</div>
        {showLog && <pre className="mono" style={{ margin: 0, maxHeight: 220, overflow: "auto", background: "var(--surface2)", padding: 10, borderRadius: 8, whiteSpace: "pre-wrap" }}>{bot.lines.join("\n") || "(nothing yet)"}</pre>}
      </div>
      <div className="card">
        <h2>How to use it — in that group, from your phone</h2>
        <div className="steps">
          <span className="n">1</span><span>Send <code>/start</code>. The bot asks: <b>1</b> = messages only, <b>2</b> = messages + link. Reply 1 or 2.</span>
          <span className="n">2</span><span>Send a <b>message list</b> as one WhatsApp message — leave a <b>blank line</b> between messages. Send more lists if you like.</span>
          <span className="n">3</span><span>Mode 2 only: right after each list, send its <b>link</b> as a separate message.</span>
          <span className="n">4</span><span>Send <code>/run</code>. The bot posts list 1 message by message, then <b>1</b>, then list 2 … then <b>2</b>, and so on. In mode 2 the link goes under every message.</span>
          <span className="n">•</span><span><code>/status</code> what it has collected · <code>/cancel</code> start over · <code>/target Other Chat</code> post the output elsewhere · <code>/help</code></span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Advanced
function AdvancedPage({ done }) {
  const [tab, setTab] = useState("recipes");
  return (
    <div className="page">
      <div><h1>Advanced</h1><p className="sub">Recipes, raw JSON, debugging and configuration.</p></div>
      <div className="tabs">{[["recipes", "Recipes"], ["edit", "Edit JSON"], ["tools", "Tools / debug"], ["config", "Config"]].map(([k, t]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>)}</div>
      {tab === "recipes" && <Recipes done={done} />}
      {tab === "edit" && <EditTask />}
      {tab === "tools" && <Tools done={done} />}
      {tab === "config" && <ConfigEditor />}
    </div>
  );
}

function Recipes({ done }) {
  const [tasks, setTasks] = useState([]); const [sel, setSel] = useState(null); const [vars, setVars] = useState({});
  const load = () => safe("list_tasks").then((t) => { setTasks(t); if (!sel && t.length) pick(t[0].name); });
  const pick = async (n) => { setSel(n); const t = await safe("get_task", n); setVars(Object.fromEntries(Object.entries(t.vars || {}).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]))); };
  useEffect(() => { load(); }, []);
  const run = async () => { const v = {}; for (const [k, s] of Object.entries(vars)) { try { v[k] = JSON.parse(s); } catch { v[k] = s; } } await safe("run_task", sel, JSON.stringify(v)); toast("queued " + sel); };
  return (
    <div className="card">
      <div className="chips">{tasks.map((t) => <span key={t.name} className={"chip " + (sel === t.name ? "on" : "")} title={t.description} onClick={() => pick(t.name)}>{t.name}</span>)}</div>
      {sel && <>
        <div className="hint">{tasks.find((t) => t.name === sel)?.description}</div>
        {Object.keys(vars).length ? <div className="split">{Object.entries(vars).map(([k, v]) => <label key={k} className="f mono">{k}<input className="mono" value={v} onChange={(e) => setVars({ ...vars, [k]: e.target.value })} /></label>)}</div> : <div className="hint">no vars</div>}
        <div className="row"><button className="btn p" onClick={run}>▶ Run</button><button className="btn" onClick={load}>↻ Reload</button></div>
      </>}
    </div>
  );
}

function EditTask() {
  const [tasks, setTasks] = useState([]); const [name, setName] = useState(""); const [json, setJson] = useState(""); const [help, setHelp] = useState({});
  useEffect(() => { safe("list_tasks").then(setTasks); safe("actions_help").then(setHelp); }, []);
  const open = async (n) => { setName(n); setJson(JSON.stringify(await safe("get_task", n), null, 2)); };
  const fresh = () => { setName("my_task"); setJson(JSON.stringify({ description: "What this does", vars: { chat: "My Test Group", message: "hello" }, steps: [{ action: "open_chat", chat: "{chat}" }, { action: "send_text", text: "{message}", delay: 1.5 }] }, null, 2)); };
  const save = async () => { let j; try { j = JSON.parse(json); } catch (e) { return toast("Invalid JSON: " + e.message); } await safe("save_task", name.trim(), JSON.stringify(j)); toast("saved"); setTasks(await safe("list_tasks")); };
  const del = async () => { if (!confirm("Delete " + name + "?")) return; await safe("delete_task", name); setTasks(await safe("list_tasks")); setName(""); setJson(""); };
  return (
    <div className="card">
      <div className="row"><div className="chips">{tasks.map((t) => <span key={t.name} className={"chip " + (name === t.name ? "on" : "")} onClick={() => open(t.name)}>{t.name}</span>)}<span className="chip" onClick={fresh}>+ new</span></div></div>
      <div className="row"><input style={{ maxWidth: 260 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="task_name" /><span className="right" /><button className="btn p" onClick={save} disabled={!name}>Save</button><button className="btn d" onClick={del} disabled={!name}>Delete</button></div>
      <textarea className="mono" rows={18} value={json} onChange={(e) => setJson(e.target.value)} spellCheck={false} />
      <details><summary className="hint">Actions cheat-sheet</summary><table style={{ marginTop: 8 }}><tbody>{Object.entries(help).map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td className="hint">{v}</td></tr>)}</tbody></table></details>
    </div>
  );
}

function Tools({ done }) {
  const [out, setOut] = useState(""); const pending = useRef(false); const [keep, setKeep] = useState(true);
  useEffect(() => { if (!pending.current) return; pending.current = false; api("result").then((r) => { if (!r?.selectors) return; let s = ""; for (const [k, v] of Object.entries(r.selectors)) { const ok = v.filter((x) => x.count > 0); s += `${k.padEnd(12)} ${ok.length ? "OK   " + ok.map((x) => x.css + " ×" + x.count).join(" | ") : "MISS"}\n`; } s += "\nEDITORS:\n" + r.editors.map((e) => JSON.stringify(e)).join("\n"); setOut(s); }); }, [done]);
  return (
    <div className="card">
      <div className="row"><button className="btn" onClick={() => safe("login")}>Open / re-login WhatsApp</button><button className="btn" onClick={() => safe("close_browser")}>Close WhatsApp window</button>
        <label className="check"><input type="checkbox" checked={keep} onChange={(e) => { setKeep(e.target.checked); safe("set_keep_browser", e.target.checked); }} />keep window open between tasks</label></div>
      <div className="row"><button className="btn" onClick={() => { pending.current = true; safe("debug", null); toast("debug queued"); }}>Debug selectors</button><span className="hint">If something can't be found on the page, run this and send the output to Claude.</span>
        {out && <button className="btn sm" onClick={() => { navigator.clipboard.writeText(out); toast("copied"); }}>copy</button>}</div>
      {out && <pre className="mono" style={{ margin: 0, maxHeight: 320, overflow: "auto", background: "var(--surface2)", padding: 10, borderRadius: 8 }}>{out}</pre>}
    </div>
  );
}

function ConfigEditor() {
  const [txt, setTxt] = useState("");
  const load = () => safe("get_config").then((c) => setTxt(JSON.stringify(c, null, 2)));
  useEffect(() => { load(); }, []);
  const save = async () => { let j; try { j = JSON.parse(txt); } catch (e) { return toast("Invalid JSON: " + e.message); } await safe("save_config", JSON.stringify(j)); toast("saved"); };
  return (
    <div className="card">
      <div className="row"><h2>config.json</h2><span className="right" /><button className="btn p" onClick={save}>Save</button><button className="btn" onClick={load}>Reload</button></div>
      <textarea className="mono" rows={20} value={txt} onChange={(e) => setTxt(e.target.value)} spellCheck={false} />
      <div className="hint">Google Sheets: set <code>sheets.sheet_id</code> and put <code>service_account.json</code> next to app.py. Bot: <code>bot.group</code>, <code>bot.headless</code>.</div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
