/* Report Maker — one script, no build step, no dependencies.
   Shell behaviour runs on every page; each page calls its own init*() at the
   bottom of its template. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CSRF = () => document.querySelector('input[name="csrf_token"]')?.value
  || document.body.dataset.csrf || "";

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 172800) return "yest";
  return `${Math.floor(s / 86400)}d`;
}
function fmtDur(s) { const m = Math.floor(s / 60); return m ? `${m}m ${s % 60}s` : `${s}s`; }
function fmtDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function api(url, opts = {}) {
  const headers = Object.assign({ "X-CSRF-Token": CSRF() }, opts.headers || {});
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(Object.assign({ csrf_token: CSRF() }, opts.json));
  }
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json().catch(() => ({})) : null;
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data ?? res;
}

/* ---------- shell: theme, menu, shortcuts, relative times ---------- */
(function shell() {
  const themeBtn = $("theme-btn");
  if (themeBtn) {
    const order = ["auto", "light", "dark"];
    const cur = () => { try { return localStorage.getItem("rm-theme") || "auto"; } catch (_) { return "auto"; } };
    const apply = (t) => {
      if (t === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = t;
      themeBtn.title = `Theme: ${t}`;
      themeBtn.textContent = t === "dark" ? "Dark" : t === "light" ? "Light" : "Auto";
    };
    apply(cur());
    themeBtn.addEventListener("click", () => {
      const next = order[(order.indexOf(cur()) + 1) % order.length];
      try { localStorage.setItem("rm-theme", next); } catch (_) {}
      apply(next);
    });
  }
  const menuBtn = $("menu-btn"), pop = $("menu-pop");
  if (menuBtn && pop) {
    menuBtn.addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; menuBtn.setAttribute("aria-expanded", String(!pop.hidden)); });
    document.addEventListener("click", () => { pop.hidden = true; });
    pop.addEventListener("click", (e) => e.stopPropagation());
  }
  // N / H / S jump between the three main pages, unless you are typing.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable)) return;
    const a = document.querySelector(`.nav a[data-key="${e.key.toLowerCase()}"]`);
    if (a && a.getAttribute("href") !== location.pathname) window.location.href = a.getAttribute("href");
  });
  const tick = () => document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = ago(+el.dataset.ago); });
  tick(); setInterval(tick, 30000);
})();

/* Rough capture-time estimate. Measured order of magnitude only: ~10 s a
   post per browser for the X engine, ~16 s for the influencer engine (it also
   visits the author's profile once per handle). Shown as "≈", never promised. */
function estimateSeconds(n, pool, workers) {
  if (!n) return 0;
  const per = pool === "influencer" ? 16 : 10;
  const w = Math.max(1, Math.min(workers || 1, n));
  return Math.round(25 + (n * per) / w);
}
function fmtEta(s) { if (!s) return ""; const m = Math.max(1, Math.round(s / 60)); return `≈ ${m} min`; }

/* =========================================================================
   New report page
   ========================================================================= */
function initSubmitForm() {
  const form = $("job-form");
  const maxLinks = +form.dataset.maxLinks;
  const defaultWorkers = +form.dataset.defaultWorkers || 1;
  const platformInput = $("platform"), typeInput = $("report-type");
  const plat = $("plat"), styles = $("styles");
  const cards = [...styles.querySelectorAll(".srow[data-slug]")];
  const input = $("file-input"), drop = $("drop"), chip = $("file-chip"), chipName = $("file-name");
  const paste = $("paste-input"), sheet = $("sheet-input"), dedupe = $("dedupe");
  const pickers = $("pickers"), pickSheet = $("pick-sheet"), pickLink = $("pick-link"), pickAccount = $("pick-account");
  const nameInput = $("report-name"), errorBox = $("form-error"), submitBtn = $("submit-btn");
  const spinner = submitBtn.querySelector(".spinner");
  const cropOption = $("crop-option"), keepEngagement = $("keep-engagement");
  const speedOption = $("speed-option"), workersSelect = $("workers");
  const summary = $("summary");
  const pv = { pill: $("prev-pill"), empty: $("prev-empty"), loading: $("prev-loading"), error: $("prev-error"),
    scroll: $("prev-scroll"), rows: $("prev-rows"), stat: $("prev-stat") };

  let activeTab = "file", userLinkCol = "", userAccountCol = "", ready = 0, lastPreview = null;
  const showError = (msg) => { errorBox.textContent = msg || ""; errorBox.hidden = !msg; };

  /* ---- platform ---- */
  const platButtons = [...plat.querySelectorAll("button[data-platform]")];
  const selectPlatform = (slug) => {
    const b = platButtons.find((x) => x.dataset.platform === slug);
    if (!b || b.classList.contains("soon")) return;
    platformInput.value = slug;
    platButtons.forEach((x) => { const on = x === b; x.classList.toggle("on", on); x.setAttribute("aria-checked", String(on)); });
    // Only styles for this platform are offered; keep the selection if it fits.
    let firstVisible = null;
    cards.forEach((c) => { const ok = c.dataset.platform === slug; c.hidden = !ok; if (ok && !firstVisible) firstVisible = c; });
    const cur = cards.find((c) => c.dataset.slug === typeInput.value);
    if (!cur || cur.hidden) selectStyle(firstVisible ? firstVisible.dataset.slug : "");
    updateSummary();
    // What counts as a link depends on the platform, so the preview re-reads.
    if (typeof schedulePreview === "function") schedulePreview(0);
  };
  platButtons.forEach((b) => b.addEventListener("click", () => selectPlatform(b.dataset.platform)));

  /* ---- style rows + sample modal ---- */
  const selectedCard = () => cards.find((c) => c.dataset.slug === typeInput.value);
  const syncOptions = () => {
    const c = selectedCard();
    cropOption.hidden = !c || c.dataset.keepEngagement !== "1";
    speedOption.hidden = !c || c.dataset.workerChoice !== "1";
  };
  const selectStyle = (slug) => {
    typeInput.value = slug;
    cards.forEach((c) => { const on = c.dataset.slug === slug; c.classList.toggle("on", on); c.setAttribute("aria-checked", String(on)); });
    syncOptions(); updateSummary();
  };
  const modal = $("sample-modal");
  const openSample = (c) => {
    if (!c || !c.dataset.large) return;
    $("sample-img").src = c.dataset.large; $("sample-open").href = c.dataset.large;
    $("sample-title").textContent = c.dataset.label; $("sample-desc").textContent = c.dataset.desc || "";
    $("sample-use").onclick = () => { selectStyle(c.dataset.slug); modal.hidden = true; };
    modal.hidden = false;
  };
  const closeSample = () => { modal.hidden = true; };
  $("sample-close").addEventListener("click", closeSample);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeSample(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSample(); });
  cards.forEach((c) => {
    c.addEventListener("click", (e) => {
      const b = e.target.closest("[data-sample]");
      if (b) { e.stopPropagation(); openSample(c); return; }
      selectStyle(c.dataset.slug);
    });
    c.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); selectStyle(c.dataset.slug); } });
  });
  workersSelect.addEventListener("change", updateSummary);

  /* ---- tabs ---- */
  const tabs = [...form.querySelectorAll(".tabs .tab[data-tab]")];
  const selectTab = (name) => {
    activeTab = name;
    tabs.forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    form.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    schedulePreview(0);
  };
  tabs.forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));

  /* ---- file ---- */
  const showFile = (file) => {
    chip.hidden = !file;
    if (!file) return;
    chipName.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
    if (!nameInput.value.trim()) nameInput.value = file.name.replace(/\.[^.]+$/, "").slice(0, 80);
  };
  input.addEventListener("change", () => { showFile(input.files[0]); schedulePreview(0); });
  $("file-clear").addEventListener("click", () => { input.value = ""; showFile(null); schedulePreview(0); });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0]; if (!file) return;
    const dt = new DataTransfer(); dt.items.add(file); input.files = dt.files;
    showFile(file); schedulePreview(0);
  });
  paste.addEventListener("input", () => schedulePreview(450));
  sheet.addEventListener("input", () => schedulePreview(700));
  sheet.addEventListener("paste", () => schedulePreview(120));
  dedupe.addEventListener("change", () => schedulePreview(0));
  pickSheet.addEventListener("change", () => { userLinkCol = ""; schedulePreview(0); });
  pickLink.addEventListener("change", () => { userLinkCol = pickLink.value; schedulePreview(0); });
  pickAccount.addEventListener("change", () => { userAccountCol = pickAccount.value; schedulePreview(0); });

  /* ---- preview ---- */
  const setReady = (n) => { ready = n; submitBtn.disabled = n <= 0; updateSummary(); };
  function updateSummary() {
    const c = selectedCard();
    const workers = c && c.dataset.workerChoice === "1" ? (+workersSelect.value || defaultWorkers) : 1;
    const eta = fmtEta(estimateSeconds(ready, c ? c.dataset.pool : "capture", workers));
    summary.innerHTML = `<b>${esc(platformLabel())}</b> · <b>${esc(c ? c.dataset.label : "—")}</b> · <b>${ready}</b> link${ready === 1 ? "" : "s"}${eta ? " · " + eta : ""}`;
    const etaEl = pv.stat.querySelector(".eta"); if (etaEl) etaEl.innerHTML = eta ? `${eta}` : "";
  }
  const platformLabel = () => { const b = platButtons.find((x) => x.classList.contains("on")); return b ? b.textContent.replace(/soon/i, "").trim() : "X"; };
  const fill = (sel, items, chosen) => {
    sel.innerHTML = "";
    for (const it of items) { const o = document.createElement("option"); o.value = String(it.value); o.textContent = it.label; if (String(it.value) === String(chosen)) o.selected = true; sel.append(o); }
  };
  const renderPickers = (data) => {
    const tabsList = data.sheets || [], cols = data.columns || [];
    $("pick-sheet-wrap").hidden = tabsList.length < 2;
    if (tabsList.length > 1) fill(pickSheet, tabsList.map((t) => ({ value: t, label: t })), data.sheet);
    const showCols = cols.length > 1;
    $("pick-link-wrap").hidden = !showCols; $("pick-account-wrap").hidden = !showCols;
    if (showCols) {
      const opts = cols.map((c) => ({ value: c.index, label: c.sample ? `${c.name} — ${c.sample.slice(0, 34)}` : c.name }));
      const gl = cols.find((c) => c.role === "link"), ga = cols.find((c) => c.role === "account");
      fill(pickLink, opts, userLinkCol !== "" ? userLinkCol : (gl ? gl.index : 0));
      fill(pickAccount, [{ value: -1, label: "— none —" }, ...opts], userAccountCol !== "" ? userAccountCol : (ga ? ga.index : -1));
    }
    pickers.hidden = $("pick-sheet-wrap").hidden && !showCols;
  };
  const setPill = (cls, text) => { pv.pill.innerHTML = `<span class="dot ${cls}"></span> ${esc(text)}`; };
  const resetPreview = () => {
    pickers.hidden = true; pv.scroll.hidden = true; pv.stat.hidden = true; pv.loading.hidden = true; pv.error.hidden = true; pv.empty.hidden = false;
    setPill("off", "nothing yet"); lastPreview = null; setReady(0);
  };
  const shortLink = (u) => String(u).replace(/^https?:\/\/(www\.)?/, "").replace(/(status\/\d{6})\d+/, "$1…");
  const renderPreview = (data) => {
    lastPreview = data;
    pv.loading.hidden = true; pv.empty.hidden = true; pv.error.hidden = true; pv.scroll.hidden = false; pv.stat.hidden = false;
    renderPickers(data);
    const dupPos = new Set(); (data.duplicates || []).forEach((d) => d.positions.slice(1).forEach((p) => dupPos.add(p)));
    const rows = [];
    let n = 0;
    for (const r of data.rows.slice(0, 200)) {
      n += 1;
      rows.push(`<tr><td>${n}</td><td>${esc(r.account || "—")}</td><td class="lnk"><span class="pp"></span><a href="${esc(r.link)}" target="_blank" rel="noopener" title="${esc(r.link)}">${esc(shortLink(r.link))}</a></td><td></td></tr>`);
    }
    if (!data.dedupe_applied) {
      (data.duplicates || []).forEach((d) => d.positions.slice(1).forEach((p) =>
        rows.push(`<tr class="dup"><td>${p}</td><td>—</td><td class="lnk">${esc(shortLink(d.link))}</td><td><span class="tag">duplicate of ${d.positions[0]}</span></td></tr>`)));
    }
    for (const d of (data.dropped || []).slice(0, 30)) {
      rows.push(`<tr class="bad"><td>${d.row}</td><td>—</td><td class="lnk" title="${esc(d.value)}">${esc(shortLink(d.value))}</td><td><span class="tag bad">${esc(d.reason.replace(/^this is not an? /, "not a "))}</span></td></tr>`);
    }
    if (data.rows.length > 200) rows.push(`<tr><td colspan="4" class="faint">…and ${data.count - 200} more</td></tr>`);
    pv.rows.innerHTML = rows.join("");
    const bits = [`<span><b>${data.count}</b> capture</span>`];
    if (data.duplicate_count) bits.push(`<span><b>${data.duplicate_count}</b> duplicate${data.dedupe_applied ? " removed" : ""}</span>`);
    if (data.dropped_count) bits.push(`<span><b>${data.dropped_count}</b> rejected</span>`);
    bits.push(`<span class="eta"></span>`);
    pv.stat.innerHTML = bits.join("");
    if (data.over_limit) {
      setPill("bad", `${data.count} links — limit is ${data.limit}`);
      pv.error.textContent = `${data.count} links, but the limit is ${data.limit} per report. Split this into smaller batches.`; pv.error.hidden = false;
      setReady(0);
    } else {
      setPill("ok", `${data.count} will be captured`);
      setReady(data.count);
    }
  };
  const showPreviewError = (msg, dropped, data) => {
    if (data && (data.sheets?.length > 1 || data.columns?.length > 1)) renderPickers(data); else pickers.hidden = true;
    pv.loading.hidden = true; pv.scroll.hidden = true; pv.stat.hidden = true; pv.empty.hidden = true;
    let html = esc(msg);
    if (dropped && dropped.length) html += `<ul style="margin:6px 0 0 16px;padding:0">${dropped.slice(0, 5).map((d) => `<li>row ${d.row}: <code>${esc(d.value)}</code></li>`).join("")}</ul>`;
    pv.error.innerHTML = html; pv.error.hidden = false;
    setPill("bad", "cannot read"); setReady(0);
  };
  let previewTimer = null, previewSeq = 0;
  function schedulePreview(delay) { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, delay); }
  function inputBody(body) {
    body.append("platform", platformInput.value || "x");
    if (userLinkCol !== "") body.append("link_col", userLinkCol);
    if (userAccountCol !== "") body.append("account_col", userAccountCol);
    if (dedupe.checked) body.append("dedupe", "1");
    if (activeTab === "file") {
      if (!input.files.length) return false;
      body.append("file", input.files[0]);
      if (!$("pick-sheet-wrap").hidden && pickSheet.value) body.append("sheet", pickSheet.value);
    } else if (activeTab === "sheet") {
      if (!sheet.value.trim()) return false;
      body.append("sheet_url", sheet.value.trim());
    } else {
      if (!paste.value.trim()) return false;
      body.append("text", paste.value);
    }
    return true;
  }
  async function runPreview() {
    const body = new FormData(); body.append("csrf_token", CSRF());
    if (!inputBody(body)) return resetPreview();
    const seq = ++previewSeq;
    pv.empty.hidden = true; pv.error.hidden = true; pv.scroll.hidden = true; pv.stat.hidden = true; pv.loading.hidden = false; setPill("warn", "reading…");
    try {
      const res = await fetch("/api/preview", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (seq !== previewSeq) return;
      if (!res.ok || !data.ok) return showPreviewError(data.detail || `Could not read that (${res.status})`, data.dropped, data);
      renderPreview(data);
    } catch (_) { if (seq === previewSeq) showPreviewError("Preview unavailable — check your connection."); }
  }

  /* ---- presets ---- */
  let presets = []; try { presets = JSON.parse($("presets-data").textContent) || []; } catch (_) {}
  const presetPick = $("preset-pick");
  const applyPreset = (p) => {
    if (!p) return;
    selectPlatform(p.platform);
    if (cards.find((c) => c.dataset.slug === p.report_type)) selectStyle(p.report_type);
    keepEngagement.checked = !!p.keep_engagement;
    workersSelect.value = p.workers ? String(p.workers) : "";
    dedupe.checked = !!p.dedupe;
    if (p.report_name) nameInput.value = p.report_name;
    if (p.sheet_url) { sheet.value = p.sheet_url; selectTab("sheet"); } else schedulePreview(0);
    updateSummary();
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const renderPresets = () => {
    presetPick.hidden = !presets.length;
    presetPick.innerHTML = `<option value="">Load a preset…</option>` + presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")
      + (presets.length ? `<option value="__manage">Delete a preset…</option>` : "");
  };
  presetPick.addEventListener("change", async () => {
    const v = presetPick.value; presetPick.value = "";
    if (v === "__manage") {
      const name = prompt("Type the exact name of the preset to delete:\n" + presets.map((p) => "• " + p.name).join("\n"));
      const p = presets.find((x) => x.name === (name || "").trim());
      if (!p) return;
      try { const r = await api(`/api/presets/${encodeURIComponent(p.id)}`, { method: "DELETE" }); presets = r.presets; renderPresets(); }
      catch (err) { showError(err.message); }
      return;
    }
    applyPreset(presets.find((p) => p.id === v));
  });
  $("preset-save").addEventListener("click", async () => {
    const c = selectedCard(); if (!c) return showError("Pick a style first.");
    const name = prompt("Preset name", nameInput.value.trim() || `${platformLabel()} · ${c.dataset.label}`);
    if (!name) return;
    try {
      const r = await api("/api/presets", { method: "POST", json: {
        name, platform: platformInput.value, report_type: typeInput.value,
        keep_engagement: keepEngagement.checked && c.dataset.keepEngagement === "1",
        workers: c.dataset.workerChoice === "1" ? (+workersSelect.value || 0) : 0,
        dedupe: dedupe.checked, sheet_url: activeTab === "sheet" ? sheet.value.trim() : "",
        report_name: nameInput.value.trim() } });
      presets = r.presets; renderPresets(); showError("");
    } catch (err) { showError(err.message); }
  });

  /* ---- submit ---- */
  form.addEventListener("submit", async (e) => {
    e.preventDefault(); showError("");
    if (!ready) return showError("Add some links first.");
    if (!typeInput.value) return showError("Pick a report style.");
    if (!nameInput.value.trim()) { nameInput.focus(); return showError("Give the report a name."); }
    submitBtn.disabled = true; spinner.hidden = false;
    const body = new FormData();
    body.append("csrf_token", CSRF());
    body.append("report_name", nameInput.value.trim());
    body.append("report_type", typeInput.value);
    inputBody(body);
    const c = selectedCard();
    if (keepEngagement.checked && c.dataset.keepEngagement === "1") body.append("keep_engagement", "1");
    if (workersSelect.value && c.dataset.workerChoice === "1") body.append("workers", workersSelect.value);
    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
      window.location.href = `/jobs/${data.job_id}`;
    } catch (err) { showError(err.message); submitBtn.disabled = false; spinner.hidden = true; }
  });

  /* ---- boot ---- */
  const q = new URLSearchParams(location.search);
  selectPlatform(platformInput.value || "x");
  const wanted = q.get("type");
  if (wanted && cards.find((c) => c.dataset.slug === wanted && !c.hidden)) selectStyle(wanted);
  const wantedPreset = q.get("preset");
  if (wantedPreset) applyPreset(presets.find((p) => p.id === wantedPreset));
  resetPreview();
}

/* =========================================================================
   Job page
   ========================================================================= */
function initJobPage(executionMode) {
  const card = $("status-card"), jobId = card.dataset.jobId;
  const pill = $("status-pill"), phase = $("phase"), wrap = $("progress-wrap"), bar = $("progress-bar");
  const counter = $("counter"), elapsed = $("elapsed"), errBox = $("job-error"), downloads = $("downloads");
  const cancelForm = $("cancel-form"), activity = $("activity"), skippedCard = $("skipped-card");
  const skippedList = $("skipped"), skippedCount = $("skipped-count"), ephemeralNote = $("ephemeral-note");
  const KINDS = ["pdf", "docx", "html", "xlsx", "zip"];

  const render = (job) => {
    pill.textContent = job.status; pill.className = `st ${job.status}`;
    phase.textContent = job.phase || "";
    elapsed.textContent = job.elapsed ? fmtDur(job.elapsed) : "";
    const running = job.status === "running" || job.status === "queued";
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    wrap.classList.toggle("indet", running && !job.done);
    wrap.hidden = job.status === "queued" && !job.total;
    bar.style.width = `${job.status === "done" ? 100 : pct}%`;
    counter.textContent = job.total ? `${Math.min(job.done, job.total)} / ${job.total} posts captured` : "";
    errBox.hidden = !job.error; errBox.textContent = job.error || "";
    if ((job.activity || []).length) {
      activity.innerHTML = job.activity.map((it) => `<li class="${esc(it.level || "info")}"><span class="ts">${new Date(it.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>${esc(it.message)}</li>`).join("");
      activity.scrollTop = activity.scrollHeight;
    }
    const sk = job.skipped || [];
    skippedCard.hidden = !sk.length; skippedCount.textContent = sk.length;
    skippedList.innerHTML = sk.map((s) => `<li><strong>${esc(s.account || s.link || "Unknown post")}</strong><span class="why">${esc(s.reason)}${s.account && s.link ? " — " + esc(s.link) : ""}</span></li>`).join("");
    const arts = job.artifacts || [];
    downloads.hidden = !arts.length;
    for (const k of KINDS) { const el = $(`dl-${k}`); if (!el) continue; el.hidden = !arts.includes(k); if (arts.includes(k)) el.href = `/api/jobs/${jobId}/download/${k}`; }
    ephemeralNote.hidden = !(arts.length && job.execution_mode === "inline");
    cancelForm.hidden = job.finished;
    document.title = `${job.status} · ${job.name} — Report Maker`;
    return job.finished;
  };
  cancelForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new FormData(); body.append("csrf_token", CSRF());
    const res = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST", body });
    if (res.ok) render(await res.json());
  });
  let delay = 1500;
  const poll = async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (res.status === 401) return (window.location.href = "/login");
      if (!res.ok) throw new Error("status unavailable");
      if (render(await res.json())) return;
      delay = Math.min(delay * 1.15, 5000);
    } catch (_) { delay = Math.min(delay * 2, 15000); }
    setTimeout(poll, delay);
  };
  const runInline = async () => {
    const res = await fetch(`/api/jobs/${jobId}/run-inline`);
    if (res.status === 409) return poll();
    if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);
    const reader = res.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n"); buffer = lines.pop();
      for (const line of lines) { if (!line.trim()) continue; try { render(JSON.parse(line)); } catch (_) {} }
    }
  };
  if (executionMode === "inline") runInline().catch(() => poll()); else poll();
}

/* =========================================================================
   History page — client-side filter over the server-rendered table, and a
   light poll while anything is still running.
   ========================================================================= */
function initHistory() {
  const q = $("hist-q"), st = $("hist-status"), ty = $("hist-type"), rows = [...document.querySelectorAll("#hist tbody tr[data-id]")];
  const count = $("hist-count");
  const apply = () => {
    const needle = (q.value || "").toLowerCase(), s = st.value, t = ty.value; let n = 0;
    rows.forEach((r) => {
      const ok = (!needle || r.dataset.text.includes(needle)) && (!s || r.dataset.status === s) && (!t || r.dataset.type === t);
      r.hidden = !ok; if (ok) n += 1;
    });
    count.textContent = `${n} of ${rows.length}`;
  };
  [q, st, ty].forEach((el) => el.addEventListener("input", apply)); apply();
  if (rows.some((r) => r.dataset.status === "running" || r.dataset.status === "queued")) setTimeout(() => location.reload(), 8000);
}

/* =========================================================================
   Report styles page — gallery actions + the designer
   ========================================================================= */
function initStyles() {
  const form = $("designer-form"); if (!form) return;
  const f = (n) => form.elements[n];
  const frame = $("dp-frame"), img = $("dp-img"), msg = $("dp-msg"), jsonBox = $("dp-json"), saveBtn = $("dp-save"), saveMsg = $("dp-save-msg");
  let opts = null;

  const num = (n, d) => { const v = parseFloat(f(n).value); return Number.isFinite(v) ? v : d; };
  const bool = (n) => !!f(n).checked;
  const checked = (name) => [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
  const engineOf = (base) => (opts?.bases || []).find((b) => b.slug === base)?.engine || "x";

  function build() {
    const base = f("extends").value;
    const engine = engineOf(base);
    const p = {
      schema: 1, slug: f("slug").value.trim(), label: f("label").value.trim(), extends: base,
      description: f("description").value.trim(),
      capture: { device_scale_factor: num("dpr", 1) },
      image: {
        max_in: [num("img_w", 3), num("img_h", 4)],
        aspect: f("aspect").value || null, fit: f("fit").value,
        background: f("background").value, radius_pt: num("radius", 0),
        border: num("border_pt", 0) > 0 ? { pt: num("border_pt", 0), color: f("border_color").value } : null,
        shadow: num("shadow_blur", 0) > 0 ? { blur_pt: num("shadow_blur", 0), opacity: num("shadow_opacity", .16), dy_pt: num("shadow_dy", 4) } : null,
      },
      page: { size: f("size").value, orientation: f("orientation").value,
        grid: [Math.max(1, Math.round(num("cols", 1))), Math.max(1, Math.round(num("rows", 1)))],
        margins_in: [num("m_top", .6), num("m_right", .6), num("m_bottom", .6), num("m_left", .6)] },
      content: { cover: bool("cover"), header: f("header").value || null, footer: f("footer").value || null,
        per_post_fields: checked("fields"), links_table: bool("links_table") },
      outputs: checked("outputs"),
    };
    if (engine === "x") p.capture.keep_engagement = bool("keep_engagement");
    if (engine === "influencer") {
      const m = checked("metrics");
      p.content.metrics = m.length ? m.map((k) => [k[0].toUpperCase() + k.slice(1), k]) : null;
    } else p.content.metrics = null;
    if (f("workers").value) p.capture.workers = +f("workers").value;
    return p;
  }
  function syncEngineUI() {
    const engine = engineOf(f("extends").value);
    $("dp-x-only").hidden = engine !== "x";
    $("dp-inf-only").hidden = engine !== "influencer";
  }
  // Auto-suggest the image box from the page geometry: the cell size less room
  // for the caption line(s). Editable — max_in is a deliberate choice (§5.4).
  function suggestBox() {
    const sizes = { letter: [8.5, 11], a4: [8.2677, 11.6929] };
    let [pw, ph] = sizes[f("size").value] || sizes.letter;
    if (f("orientation").value === "landscape") [pw, ph] = [ph, pw];
    const cols = Math.max(1, num("cols", 1)), rows = Math.max(1, num("rows", 1));
    const cw = (pw - num("m_left", .6) - num("m_right", .6) - 0.25 * (cols - 1)) / cols;
    const caption = 0.35 * Math.max(1, checked("fields").length) + (engineOf(f("extends").value) === "influencer" ? 1.1 : 0);
    const ch = (ph - num("m_top", .6) - num("m_bottom", .6) - 0.6 - 0.25 * (rows - 1)) / rows - caption;
    f("img_w").value = Math.max(0.5, cw).toFixed(2);
    f("img_h").value = Math.max(0.5, ch).toFixed(2);
  }
  let timer = null, seq = 0;
  async function preview() {
    const p = build();
    jsonBox.textContent = JSON.stringify(p, null, 2);
    const mine = ++seq;
    msg.textContent = "Drawing…"; msg.className = "small faint";
    try {
      const res = await fetch("/api/styles/preview", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF() },
        body: JSON.stringify({ csrf_token: CSRF(), profile: p, width: 260 }) });
      if (mine !== seq) return;
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `Preview failed (${res.status})`); }
      const blob = await res.blob();
      img.src = URL.createObjectURL(blob); img.hidden = false; frame.classList.remove("err");
      msg.textContent = "Drawn from the profile's real geometry — same code as the dashboard cards."; msg.className = "small faint"; msg.style.color = "";
      saveBtn.disabled = false;
    } catch (err) {
      frame.classList.add("err"); msg.textContent = err.message; msg.className = "small"; msg.style.color = "var(--bad)";
      saveBtn.disabled = true;
    }
  }
  const schedule = () => { clearTimeout(timer); timer = setTimeout(preview, 350); };
  form.addEventListener("input", (e) => {
    if (e.target.name === "label" && !f("slug").dataset.touched) f("slug").value = f("label").value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
    if (e.target.name === "slug") f("slug").dataset.touched = "1";
    if (["size", "orientation", "cols", "rows", "m_top", "m_right", "m_bottom", "m_left"].includes(e.target.name)) suggestBox();
    if (e.target.name === "extends") { syncEngineUI(); suggestBox(); }
    schedule();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault(); saveMsg.textContent = ""; saveBtn.disabled = true;
    try {
      const r = await api("/api/styles", { method: "POST", json: { profile: build(), overwrite: bool("overwrite") } });
      saveMsg.innerHTML = `<span class="alert alert-ok tight">Saved <b>${esc(r.label)}</b>. It is now a card on the New report page.</span>`;
      setTimeout(() => location.assign(`/styles#style-${encodeURIComponent(r.slug)}`), 600);
      setTimeout(() => location.reload(), 700);
    } catch (err) { saveMsg.innerHTML = `<span class="alert alert-error tight">${esc(err.message)}</span>`; saveBtn.disabled = false; }
  });
  // Load an existing profile into the form ("duplicate" / "edit").
  async function loadInto(slug, asCopy) {
    try {
      const r = await api(`/api/styles/${encodeURIComponent(slug)}`);
      const p = r.raw, cap = p.capture || {}, im = p.image || {}, pg = p.page || {}, ct = p.content || {};
      const base = p.extends || (r.custom ? "twitter" : slug);
      f("extends").value = (opts.bases.some((b) => b.slug === base)) ? base : "twitter";
      f("label").value = asCopy ? `${p.label} copy` : p.label;
      f("slug").value = asCopy ? "" : p.slug; f("slug").dataset.touched = asCopy ? "" : "1";
      if (asCopy) f("slug").value = f("label").value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
      f("description").value = p.description || "";
      f("overwrite").checked = !asCopy && r.custom;
      if (cap.device_scale_factor) f("dpr").value = cap.device_scale_factor;
      f("keep_engagement").checked = !!cap.keep_engagement;
      f("workers").value = cap.workers || "";
      if (im.max_in) { f("img_w").value = im.max_in[0]; f("img_h").value = im.max_in[1]; }
      f("aspect").value = im.aspect || ""; f("fit").value = im.fit || "fit";
      f("background").value = im.background || "#FFFFFF"; f("radius").value = im.radius_pt || 0;
      f("border_pt").value = im.border ? im.border.pt : 0; f("border_color").value = im.border?.color || "#E1E8ED";
      f("shadow_blur").value = im.shadow ? im.shadow.blur_pt : 0; f("shadow_opacity").value = im.shadow?.opacity ?? .16; f("shadow_dy").value = im.shadow?.dy_pt ?? 4;
      f("size").value = String(pg.size || "letter").toLowerCase(); f("orientation").value = pg.orientation || "portrait";
      if (pg.grid) { f("cols").value = pg.grid[0]; f("rows").value = pg.grid[1]; }
      if (pg.margins_in) ["m_top", "m_right", "m_bottom", "m_left"].forEach((n, i) => { f(n).value = pg.margins_in[i]; });
      f("cover").checked = !!ct.cover; f("header").value = ct.header || ""; f("footer").value = ct.footer || "";
      f("links_table").checked = ct.links_table !== false;
      form.querySelectorAll('input[name="fields"]').forEach((i) => { i.checked = (ct.per_post_fields || []).includes(i.value); });
      form.querySelectorAll('input[name="metrics"]').forEach((i) => { i.checked = (ct.metrics || []).some((m) => m[1] === i.value); });
      form.querySelectorAll('input[name="outputs"]').forEach((i) => { i.checked = (p.outputs || []).includes(i.value); });
      syncEngineUI(); schedule();
      $("designer").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) { saveMsg.innerHTML = `<span class="alert alert-error tight">${esc(err.message)}</span>`; }
  }
  document.querySelectorAll("[data-dup]").forEach((b) => b.addEventListener("click", () => loadInto(b.dataset.dup, true)));
  document.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => loadInto(b.dataset.edit, false)));
  document.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Delete the style "${b.dataset.label}"? Reports already made with it are unaffected.`)) return;
    try { await api(`/api/styles/${encodeURIComponent(b.dataset.delete)}`, { method: "DELETE" }); location.reload(); }
    catch (err) { alert(err.message); }
  }));
  // Boot: vocab from the server, then first draw.
  api("/api/styles").then((r) => {
    opts = r.options;
    f("extends").innerHTML = opts.bases.map((b) => `<option value="${esc(b.slug)}">${esc(b.label)} (${b.engine} engine)</option>`).join("");
    f("extends").value = "twitter";
    syncEngineUI(); suggestBox(); preview();
    const h = location.hash;
    if (h.startsWith("#dup=")) loadInto(decodeURIComponent(h.slice(5)), true);
    if (h.startsWith("#edit=")) loadInto(decodeURIComponent(h.slice(6)), false);
    if (h === "#designer") $("designer").scrollIntoView();
  }).catch((err) => { msg.textContent = err.message; });
}
