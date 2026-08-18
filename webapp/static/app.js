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

  /* v3: the project switcher in the left bar + the New project dialog. */
  const pdBtn = $("pdrop-btn"), pdList = $("pdrop-list");
  if (pdBtn && pdList) {
    const open = (on) => { pdList.hidden = !on; pdBtn.setAttribute("aria-expanded", String(on)); };
    pdBtn.addEventListener("click", (e) => { e.stopPropagation(); open(pdList.hidden); });
    document.addEventListener("click", () => open(false));
    pdList.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
    pdList.querySelectorAll("button[data-pid]").forEach((b) => b.addEventListener("click", async () => {
      if (b.classList.contains("on")) return open(false);
      b.disabled = true;
      try { await api(`/api/projects/${encodeURIComponent(b.dataset.pid)}/select`, { method: "POST", json: {} }); location.reload(); }
      catch (err) { alert(err.message); b.disabled = false; }
    }));
    const modal = $("np-modal"), form = $("np-form"), msg = $("np-msg");
    const show = (on) => { modal.hidden = !on; open(false); if (on) setTimeout(() => form.elements.name.focus(), 30); };
    $("pdrop-new").addEventListener("click", () => show(true));
    $("np-close").addEventListener("click", () => show(false));
    modal.addEventListener("click", (e) => { if (e.target === modal) show(false); });
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); msg.textContent = "";
      const name = form.elements.name.value.trim();
      if (name.length < 2) { msg.textContent = "Give it a name."; return; }
      try {
        await api("/api/projects", { method: "POST", json: { name, client: form.elements.client.value.trim(), emoji: form.elements.emoji.value.trim() } });
        location.href = "/project/styles";          // first stop: pick what it prints in
      } catch (err) { msg.textContent = err.message; msg.style.color = "var(--bad)"; }
    });
  }
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
  const cards = styles ? [...styles.querySelectorAll(".srow[data-slug]")] : [];
  let settings = {}; try { settings = JSON.parse(form.dataset.settings || "{}") || {}; } catch (_) {}
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
    updateSummary();
    // What counts as a link depends on the platform, so the preview re-reads.
    if (typeof schedulePreview === "function") schedulePreview(0);
  };
  platButtons.forEach((b) => b.addEventListener("click", () => selectPlatform(b.dataset.platform)));

  /* ---- style rows (tick one or more) + sample modal ---- */
  const selectedCards = () => cards.filter((c) => c.classList.contains("on"));
  const selectedCard = () => selectedCards()[0];

  /* Outputs. The formats belong to the STYLE, so the row is rebuilt whenever
     the style changes and never carries a tick over to a style that cannot
     produce it — the server refuses that anyway (report_types.check_outputs),
     but offering it would be a promise the job could not keep. */
  const OUTPUT_NOTE = { pdf: "exact, print-ready", docx: "editable in Word",
    pptx: "editable deck — every slot a movable object" };
  const outputsBox = $("outputs"), outputsOption = $("outputs-option");
  const styleOutputs = (c) => (c && c.dataset.outputs ? c.dataset.outputs.split(",").filter(Boolean) : []);
  // v3: formats were chosen per style on the project's Styles page. The union
  // of the ticked styles' formats rides along as hidden inputs; the server
  // keeps, per job, the ones that style builds (clean_outputs).
  const checkedOutputs = () => { const u = []; selectedCards().forEach((c) => styleOutputs(c).forEach((o) => { if (!u.includes(o)) u.push(o); })); return u; };
  const renderOutputs = () => {
    const sel = selectedCards();
    outputsOption.hidden = !sel.length;
    outputsBox.innerHTML = sel.map((c) => `<span class="small" style="margin-right:12px"><b>${esc(c.dataset.label)}</b> ${styleOutputs(c).map((o) => `<span class="tag" title="${esc(OUTPUT_NOTE[o] || "")}">${esc(o.toUpperCase())}</span>`).join(" ")}</span>`).join("")
      + checkedOutputs().map((o) => `<input type="hidden" name="outputs" value="${esc(o)}">`).join("");
  };

  const syncOptions = () => {
    const sel = selectedCards();
    cropOption.hidden = !sel.some((c) => c.dataset.keepEngagement === "1");
    speedOption.hidden = !sel.some((c) => c.dataset.workerChoice === "1");
  };
  const platNote = $("plat-note");
  /* Every ticked style must capture from the same network (one link list, one
     platform per run). The first tick decides; styles of another platform are
     greyed out until it is unticked. */
  const syncCards = () => {
    const sel = selectedCards();
    const platform = sel.length ? sel[0].dataset.platform : "";
    cards.forEach((c) => {
      const off = !!platform && c.dataset.platform !== platform;
      c.classList.toggle("off", off); c.setAttribute("aria-disabled", String(off));
    });
    typeInput.value = sel.map((c) => c.dataset.slug).join(",");
    if (platform && platformInput.value !== platform) selectPlatform(platform);
    if (platNote) platNote.textContent = platform ? `Capturing ${platformLabel()} links${cards.some((c) => c.classList.contains("off")) ? " — styles for other networks are greyed out until you untick these" : ""}.` : "Tick a style to start.";
  };
  const toggleStyle = (slug, force) => {
    const c = cards.find((x) => x.dataset.slug === slug); if (!c) return;
    if (c.classList.contains("off")) return;
    const on = force === undefined ? !c.classList.contains("on") : !!force;
    c.classList.toggle("on", on); c.setAttribute("aria-checked", String(on));
    syncCards(); syncOptions(); renderOutputs(); updateSummary();
  };
  const selectStyle = (slug) => toggleStyle(slug, true);
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
      toggleStyle(c.dataset.slug);
    });
    c.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleStyle(c.dataset.slug); } });
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
    const sel = selectedCards(), c = sel[0];
    const workers = c && c.dataset.workerChoice === "1" ? (+workersSelect.value || defaultWorkers) : 1;
    const eta = fmtEta(estimateSeconds(ready, c ? c.dataset.pool : "capture", workers) * Math.max(1, sel.length));
    const outs = checkedOutputs().map((o) => o.toUpperCase()).join(" + ");
    const what = sel.length === 0 ? "—" : sel.length === 1 ? c.dataset.label : `${sel.length} styles`;
    summary.innerHTML = `<b>${esc(platformLabel())}</b> · <b>${esc(what)}</b> · <b>${ready}</b> link${ready === 1 ? "" : "s"}${outs ? " · " + esc(outs) : ""}${eta ? " · " + eta : ""}`;
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
      const m = r.metrics && Object.keys(r.metrics).length ? Object.entries(r.metrics).map(([k, v]) => `${k} ${v}`).join(" · ") : "";
      rows.push(`<tr><td>${n}</td><td>${esc(r.account || "—")}${r.section ? `<br><span class="faint" style="font-size:10px">${esc(r.section)}</span>` : ""}</td><td class="lnk"><span class="pp ${esc(r.platform || "x")}"></span><a href="${esc(r.link)}" target="_blank" rel="noopener" title="${esc(r.link)}">${esc(shortLink(r.link))}</a></td><td>${m ? `<span class="tag" title="metrics from the sheet">${esc(m)}</span>` : ""}</td></tr>`);
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

  /* ---- submit ---- */
  form.addEventListener("submit", async (e) => {
    e.preventDefault(); showError("");
    if (!ready) return showError("Add some links first.");
    const sel = selectedCards();
    if (!sel.length) return showError("Tick at least one style.");
    if (!nameInput.value.trim()) { nameInput.focus(); return showError("Give the report a name."); }
    submitBtn.disabled = true; spinner.hidden = false;
    const body = new FormData();
    body.append("csrf_token", CSRF());
    body.append("project_id", form.elements.project_id.value);
    body.append("report_name", nameInput.value.trim());
    sel.forEach((c) => body.append("report_type", c.dataset.slug));
    inputBody(body);
    if (keepEngagement.checked && sel.some((c) => c.dataset.keepEngagement === "1")) body.append("keep_engagement", "1");
    if (workersSelect.value && sel.some((c) => c.dataset.workerChoice === "1")) body.append("workers", workersSelect.value);
    checkedOutputs().forEach((o) => body.append("outputs", o));
    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
      window.location.href = (data.job_ids && data.job_ids.length > 1) ? "/runs" : `/jobs/${data.job_id}`;
    } catch (err) { showError(err.message); submitBtn.disabled = false; spinner.hidden = true; }
  });

  /* ---- boot ---- */
  const q = new URLSearchParams(location.search);
  // Project defaults, then the URL, then: one style → ticked for you.
  if (settings.dedupe === false) dedupe.checked = false;
  if (settings.keep_engagement) keepEngagement.checked = true;
  if (settings.workers) workersSelect.value = String(settings.workers);
  const wanted = (q.get("type") || "").split(",").filter(Boolean);
  wanted.forEach((w) => selectStyle(w));
  if (!selectedCards().length && cards.length === 1) selectStyle(cards[0].dataset.slug);
  if (!selectedCards().length) { selectPlatform(cards[0] ? cards[0].dataset.platform : "x"); syncCards(); renderOutputs(); }
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
  const KINDS = ["pdf", "docx", "pptx", "xlsx", "zip"];

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
        margins_in: [num("m_top", .6), num("m_right", .6), num("m_bottom", .6), num("m_left", .6)],
        // v3: a page background (colour) for the PDF and the PPTX. An image
        // background is set from the project's Styles page, which stores the
        // file; the designer only carries the colour.
        background: bool("page_bg_on") ? { color: f("page_bg").value.toUpperCase() } : null },
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
      f("page_bg_on").checked = !!(pg.background && pg.background.color);
      f("page_bg").value = (pg.background && pg.background.color) || "#FFFFFF";
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
  document.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => { if (b.dataset.tpl !== "1") loadInto(b.dataset.edit, false); }));
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

/* =========================================================================
   Users & roles (admin)
   ========================================================================= */
function initUsers() {
  const form = $("user-add"); if (!form) return;
  const msg = $("user-msg");
  const say = (t, ok) => { msg.textContent = t; msg.style.color = ok ? "var(--ok)" : "var(--bad)"; };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    try {
      await api("/api/users", { method: "POST", json: { username: f.get("username"), password: f.get("password"), role: f.get("role") } });
      location.reload();
    } catch (err) { say(err.message, false); }
  });
  document.querySelectorAll(".role-pick").forEach((sel) => sel.addEventListener("change", async () => {
    try { await api(`/api/users/${encodeURIComponent(sel.dataset.user)}`, { method: "PATCH", json: { role: sel.value } }); say(`Role updated for ${sel.dataset.user}.`, true); }
    catch (err) { say(err.message, false); location.reload(); }
  }));
  document.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
    const pw = prompt(`New password for ${b.dataset.reset} (8+ characters):`); if (!pw) return;
    try { await api(`/api/users/${encodeURIComponent(b.dataset.reset)}`, { method: "PATCH", json: { password: pw } }); say(`Password reset for ${b.dataset.reset}.`, true); }
    catch (err) { say(err.message, false); }
  }));
  document.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Remove ${b.dataset.remove}? Their reports stay in History.`)) return;
    try { await api(`/api/users/${encodeURIComponent(b.dataset.remove)}`, { method: "DELETE" }); location.reload(); }
    catch (err) { say(err.message, false); }
  }));
}

/* =========================================================================
   Report styles: admin curation toggles (works on any page that has them)
   ========================================================================= */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-vis]"); if (!b) return;
  b.disabled = true;
  try { await api(`/api/styles/${encodeURIComponent(b.dataset.vis)}/visibility`, { method: "POST", json: { show: b.dataset.show === "1" } }); location.reload(); }
  catch (err) { alert(err.message); b.disabled = false; }
});

/* v3: "Add to <project>" on a pool card — appends the style to the current
   project's list (every file it builds), then reloads so the card says so. */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-addproj]"); if (!b) return;
  b.disabled = true;
  try {
    const cur = await api("/api/projects");
    const list = (cur.current.styles || []).filter((s) => !s.missing).map((s) => ({ slug: s.slug, outputs: s.outputs }));
    if (!list.some((s) => s.slug === b.dataset.addproj)) list.push({ slug: b.dataset.addproj, outputs: [] });
    await api(`/api/projects/${encodeURIComponent(cur.current.id)}/styles`, { method: "PUT", json: { styles: list } });
    location.reload();
  } catch (err) { alert(err.message); b.disabled = false; }
});

/* =========================================================================
   Template designer — a designed page (Canva PNG) + slots drawn on it.
   State: pages {post|cover|summary|end: {file, url, w, h, ghost}}, items[] with
   fractional x/y/w/h, kind 'slot' | 'logo' | 'summary' | 'text', and for text:
   field/size/color/align/bold/font/page.

   v2.3 "design kit": every picture the designer looks at — the Canva slot
   guide and the live page preview — is drawn by the SERVER from this same
   meta, so nothing here re-implements the page. See RULEBOOK §18a.
   ========================================================================= */
function initTemplateDesigner() {
  const canvas = $("tcanvas"); if (!canvas) return;
  const img = $("t-img"), layer = $("tlayer"), empty = $("t-empty"), status = $("t-status");
  const props = $("t-props"), fileIn = $("t-file"), removeBtn = $("t-remove");
  const pages = { post: null, cover: null, summary: null, end: null };
  let cur = "post", items = [], sel = null, editingSlug = "", copyFrom = "", fonts = [];
  const LABELS = { title: "Report title", date: "Date", page: "Page no.", pages: "Pages", index: "#", account_name: "Account", post_link: "Post URL", category: "Category", metrics: "Metrics",
    handle: "Handle name", section: "Section", post_no: "Post 1", post_total: "Top 9 Posts", post_total_n: "9 Posts", platform: "Platform", link: "LINK",
    "metric.like": "Like →", "metric.impressions": "Impressions →", "metric.views": "Views →", "metric.reach": "Reach →", "metric.comments": "Comments →", "metric.shares": "Shares →", "metric.followers": "Followers →" };
  const PAGE_KINDS = ["post", "cover", "summary", "end"];
  // Same numbers the server renders at: slides at 144 dpi (16:9 -> 1920x1080),
  // paper at 150. Shown so a designer picks the right Canva page size.
  const PAPER_IN = { "16:9": [7.5, 13.3333], "4:3": [7.5, 10], a4: [8.2677, 11.6929], letter: [8.5, 11] };
  const TEXT_PRESETS = {
    pill: { size: 18, color: "#ffffff", align: "center", bold: true },
    heading: { size: 22, bold: true },
    label: { size: 11, bold: false },
  };

  const setStatus = (t) => { status.textContent = t; };
  const pageTag = (k) => $(`t-has-${k}`);
  const refreshTabs = () => {
    for (const k of PAGE_KINDS) {
      const t = pageTag(k); if (!t) continue;
      t.textContent = pages[k] ? (pages[k].ghost ? "✓ borrowed" : "✓ image") : (k === "post" ? "required" : "optional");
      t.className = "tag" + (pages[k] ? " new" : "");
    }
    removeBtn.hidden = !pages[cur] || cur === "post";
  };
  const showPage = (k) => {
    cur = k;
    document.querySelectorAll("[data-tpage]").forEach((b) => b.classList.toggle("on", b.dataset.tpage === k));
    const p = pages[k];
    img.hidden = !p; empty.hidden = !!p;
    if (p) img.src = p.url;
    canvas.classList.toggle("ghosted", !!(p && p.ghost));
    empty.innerHTML = k === "post" ? "Drop a PNG here<br><small>16:9 slide, A4 or Letter page exported from Canva — the art only, no numbers or names</small>"
      : `No ${k} page yet — upload one to add it<br><small>Optional. Cover first, Summary (section counts) second, End page last.</small>`;
    select(null); render(); refreshTabs(); schedulePreview(0);
  };
  // What to pick in Canva, and how big the guide comes out — derived from the
  // same page table the server renders with, never typed in twice.
  function guidePixels() {
    const key = $("t-paper").value, land = $("t-orient").value === "landscape";
    const dpi = (key === "16:9" || key === "4:3") ? 144 : 150;
    let [w, h] = PAPER_IN[key] || PAPER_IN.letter;
    if (land) [w, h] = [h, w];
    return [Math.round(w * dpi), Math.round(h * dpi)];
  }
  function syncKitNote() {
    const [w, h] = guidePixels(), key = $("t-paper").value;
    const name = key === "16:9" ? "Presentation (16:9)" : key === "4:3" ? "Presentation (4:3)"
      : key === "a4" ? "A4 document" : "US Letter document";
    $("t-canva-size").textContent = `${name} — ${w} × ${h} px`;
  }
  document.querySelectorAll("[data-tpage]").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.tpage)));

  const loadFile = (file, k = cur) => {
    if (!file || !/^image\/(png|jpeg)$/.test(file.type)) return setStatus("PNG or JPEG only.");
    const url = URL.createObjectURL(file);
    const probe = new Image();
    probe.onload = () => {
      pages[k] = { file, url, w: probe.naturalWidth, h: probe.naturalHeight, ghost: false };
      if (k === "post") {                       // paper follows the art
        const r = probe.naturalWidth / probe.naturalHeight, land = r > 1, a = land ? r : 1 / r;
        const paper = Math.abs(a - 16 / 9) < 0.06 ? "16:9" : Math.abs(a - 4 / 3) < 0.06 ? "4:3" : Math.abs(a - 297 / 210) < 0.05 ? "a4" : "letter";
        $("t-paper").value = paper; $("t-orient").value = land ? "landscape" : "portrait";
        syncKitNote();
      }
      showPage(k); setStatus(`${k} page: ${probe.naturalWidth}×${probe.naturalHeight}px${k === "post" ? ` → ${$("t-paper").value} ${$("t-orient").value}. Now add a screenshot slot (or "Place standard slots").` : "."}`);
    };
    probe.src = url;
  };
  fileIn.addEventListener("change", () => { loadFile(fileIn.files[0]); fileIn.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => canvas.addEventListener(ev, (e) => { e.preventDefault(); canvas.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) => canvas.addEventListener(ev, (e) => { e.preventDefault(); canvas.classList.remove("dragover"); }));
  canvas.addEventListener("drop", (e) => { const f = e.dataTransfer?.files?.[0]; if (f) loadFile(f); });
  removeBtn.addEventListener("click", () => { pages[cur] = null; items = items.filter((it) => it.page !== cur); showPage(cur); });

  /* ---- items ---- */
  const visible = () => items.filter((it) => (it.kind === "slot" || it.kind === "logo") ? cur === "post" : it.kind === "summary" ? cur === "summary" : (it.page === cur || it.page === "all"));
  const el = (it) => layer.querySelector(`[data-id="${it.id}"]`);
  let nextId = 1;
  function add(kind, field, box) {
    const it = { id: nextId++, kind, page: cur, ...box };
    if (kind === "text") Object.assign(it, { field, size: field === "title" ? 20 : 11, color: "#111111", align: "left", bold: field === "title", font: "Helvetica" });
    items.push(it); render(); select(it);
  }
  const rectFrac = (x1, y1, x2, y2) => {
    const r = layer.getBoundingClientRect();
    const fx = (v) => Math.min(1, Math.max(0, (v - r.left) / r.width)), fy = (v) => Math.min(1, Math.max(0, (v - r.top) / r.height));
    const x = Math.min(fx(x1), fx(x2)), y = Math.min(fy(y1), fy(y2));
    return { x, y, w: Math.max(0.02, Math.abs(fx(x2) - fx(x1))), h: Math.max(0.02, Math.abs(fy(y2) - fy(y1))) };
  };
  document.querySelector("[data-add='slot']").addEventListener("click", () => {
    if (!pages.post) return setStatus("Upload the post page first.");
    if (cur !== "post") showPage("post");
    add("slot", null, { x: 0.1, y: 0.15, w: 0.8, h: 0.6 });
  });
  document.querySelector("[data-add='logo']").addEventListener("click", () => {
    if (!pages.post) return setStatus("Upload the post page first.");
    if (cur !== "post") showPage("post");
    add("logo", null, { x: 0.69, y: 0.14, w: 0.075, h: 0.13 });
  });
  document.querySelector("[data-add='summary']").addEventListener("click", () => {
    if (!pages.summary && !pages.post) return setStatus("Upload a page first.");
    if (cur !== "summary") showPage("summary");
    if (items.some((it) => it.kind === "summary")) return setStatus("There is already a summary table box.");
    add("summary", null, { x: 0.36, y: 0.45, w: 0.6, h: 0.5 });
  });
  // The Kashi-style layout in one click; nudge afterwards.
  $("t-preset").addEventListener("click", () => {
    if (!pages.post) return setStatus("Upload the post page first.");
    showPage("post"); items = items.filter((it) => it.page !== "post" && it.kind !== "slot" && it.kind !== "logo");
    const T = (field, x, y, w, h, size, color, align, bold) => items.push({ id: nextId++, kind: "text", page: "post", field, x, y, w, h, size, color, align, bold });
    items.push({ id: nextId++, kind: "slot", page: "post", x: 0.695, y: 0.31, w: 0.255, h: 0.66 });
    items.push({ id: nextId++, kind: "logo", page: "post", x: 0.69, y: 0.145, w: 0.075, h: 0.135 });
    T("section", 0.08, 0.135, 0.32, 0.04, 11, "#3d3d3d", "center", false);
    T("handle", 0.06, 0.175, 0.36, 0.06, 22, "#e8571c", "center", true);
    T("date", 0.10, 0.255, 0.28, 0.04, 11, "#e8571c", "center", false);
    T("post_total", 0.80, 0.165, 0.16, 0.04, 12, "#333333", "left", false);
    T("post_no", 0.80, 0.195, 0.16, 0.08, 30, "#111111", "left", true);
    T("metric.like", 0.555, 0.482, 0.11, 0.04, 18, "#ffffff", "center", true);
    T("metric.impressions", 0.555, 0.562, 0.11, 0.04, 18, "#ffffff", "center", true);
    T("metric.views", 0.555, 0.642, 0.11, 0.04, 18, "#ffffff", "center", true);
    T("link", 0.555, 0.722, 0.11, 0.04, 16, "#ffffff", "center", true);
    T("post_total", 0.18, 0.68, 0.14, 0.05, 14, "#ffffff", "center", true);
    render(); setStatus("Standard slots placed. Drag any box to match your art; ⌫ deletes the selected one.");
  });
  document.querySelectorAll("[data-add-text]").forEach((b) => b.addEventListener("click", () => {
    if (!pages[cur]) return setStatus("Upload this page's image first.");
    add("text", b.dataset.addText, { x: 0.08, y: 0.05, w: 0.6, h: 0.04 });
  }));

  function render(guides) {
    layer.innerHTML = "";
    for (const g of guides || []) {
      const l = document.createElement("i");
      l.className = `tguide ${g[0]}`;
      l.style[g[0] === "v" ? "left" : "top"] = `${g[1] * 100}%`;
      layer.append(l);
    }
    for (const it of visible()) {
      const d = document.createElement("div");
      d.className = `titem ${it.kind}${sel === it ? " sel" : ""}`;
      d.dataset.id = it.id;
      d.style.left = `${it.x * 100}%`; d.style.top = `${it.y * 100}%`; d.style.width = `${it.w * 100}%`; d.style.height = `${it.h * 100}%`;
      if (it.kind === "slot") {
        const n = items.filter((s) => s.kind === "slot").indexOf(it) + 1;
        d.innerHTML = `<span class="lbl">Screenshot ${n}</span>`;
      } else if (it.kind === "logo") {
        d.innerHTML = `<span class="lbl">Logo</span>`;
      } else if (it.kind === "summary") {
        d.innerHTML = `<span class="lbl">Summary table (sections → counts)</span>`;
      } else {
        d.innerHTML = `<span class="lbl" style="font-weight:${it.bold ? 700 : 400};text-align:${it.align};color:${it.color}">${esc(LABELS[it.field] || it.field)}</span>`;
      }
      d.innerHTML += `<i class="h"></i>`;
      layer.append(d);
    }
    const n = items.filter((it) => it.kind === "slot").length;
    $("t-perpage").textContent = n ? `${n} post${n === 1 ? "" : "s"} per page` : "no screenshot slot yet";
    setSaveable(); schedulePreview();
  }
  function select(it) {
    sel = it;
    layer.querySelectorAll(".titem").forEach((d) => d.classList.toggle("sel", it && +d.dataset.id === it.id));
    props.hidden = !it;
    if (!it) return;
    const isText = it.kind === "text";
    props.querySelectorAll("[data-text-only]").forEach((f) => { f.hidden = !isText; });
    $("tp-what").textContent = isText ? (LABELS[it.field] || it.field)
      : it.kind === "slot" ? "screenshot slot" : it.kind === "logo" ? "platform logo" : "summary table";
    syncNums();
    if (isText) {
      $("tp-size").value = it.size; $("tp-color").value = it.color;
      $("tp-align").value = it.align; $("tp-bold").checked = !!it.bold;
      renderFontOptions(); $("tp-font").value = it.font || "Helvetica";
    }
  }
  /* numeric X / Y / W / H, in % — typed instead of dragged when a value has to
     match the art exactly */
  const NUMS = [["tp-x", "x"], ["tp-y", "y"], ["tp-w", "w"], ["tp-h", "h"]];
  // `except` is the field being typed into: rewriting it mid-keystroke would
  // turn "50" into "5.00" as soon as the "5" landed.
  const syncNums = (except) => {
    if (!sel) return;
    NUMS.forEach(([id, k]) => { if (id !== except) $(id).value = (sel[k] * 100).toFixed(1); });
  };
  NUMS.forEach(([id, k]) => $(id).addEventListener("input", () => {
    if (!sel) return;
    const v = parseFloat($(id).value); if (!Number.isFinite(v)) return;
    const f = v / 100;
    if (k === "x") sel.x = Math.min(Math.max(0, f), 1 - sel.w);
    else if (k === "y") sel.y = Math.min(Math.max(0, f), 1 - sel.h);
    else if (k === "w") sel.w = Math.min(Math.max(0.02, f), 1 - sel.x);
    else sel.h = Math.min(Math.max(0.02, f), 1 - sel.y);
    render(); syncNums(id);        // render() keeps the box selected: sel === it
  }));
  // A value that had to be clamped (x 99% on a 35%-wide box) is corrected in
  // the field the moment you leave it, so the numbers never lie about the box.
  NUMS.forEach(([id]) => $(id).addEventListener("blur", () => syncNums()));
  $("tp-size").addEventListener("input", () => { if (sel) { sel.size = +$("tp-size").value || 10; render(); } });
  $("tp-color").addEventListener("input", () => { if (sel) { sel.color = $("tp-color").value; render(); } });
  $("tp-align").addEventListener("change", () => { if (sel) { sel.align = $("tp-align").value; render(); } });
  $("tp-bold").addEventListener("change", () => { if (sel) { sel.bold = $("tp-bold").checked; render(); } });
  $("tp-font").addEventListener("change", () => { if (sel) { sel.font = $("tp-font").value; render(); } });
  document.querySelectorAll("[data-preset-text]").forEach((b) => b.addEventListener("click", () => {
    if (!sel || sel.kind !== "text") return;
    Object.assign(sel, TEXT_PRESETS[b.dataset.presetText]);
    select(sel); render();
  }));
  const del = () => { if (!sel) return; items = items.filter((it) => it !== sel); select(null); render(); };
  const dup = () => {
    if (!sel) return;
    const copy = { ...sel, id: nextId++, x: Math.min(1 - sel.w, sel.x + 0.02), y: Math.min(1 - sel.h, sel.y + 0.02) };
    items.push(copy); render(); select(copy);
  };
  $("tp-del").addEventListener("click", del);
  $("tp-dup").addEventListener("click", dup);
  document.addEventListener("keydown", (e) => {
    if (props.hidden || !sel) return;
    // Duplicate is not a text-editing key, so it works even from the X/Y/W/H
    // fields; ⌫ and the arrows must not, or typing a number would move a box.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "d") { e.preventDefault(); return dup(); }
    if (/^(input|textarea|select)$/i.test(e.target.tagName) || e.target.isContentEditable) return;
    if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); return del(); }
    const step = e.shiftKey ? 10 : 1;              // pixels on screen, not %
    const r = layer.getBoundingClientRect();
    const dx = { ArrowLeft: -step, ArrowRight: step }[e.key] || 0;
    const dy = { ArrowUp: -step, ArrowDown: step }[e.key] || 0;
    if (!dx && !dy) return;
    e.preventDefault();
    sel.x = Math.min(1 - sel.w, Math.max(0, sel.x + dx / r.width));
    sel.y = Math.min(1 - sel.h, Math.max(0, sel.y + dy / r.height));
    render(); select(sel);
  });

  /* ---- fonts: up to 3 uploaded files, offered to every text slot ---- */
  function renderFontOptions() {
    const cursel = $("tp-font").value;
    $("tp-font").innerHTML = ['<option value="Helvetica">Helvetica</option>']
      .concat(fonts.map((f) => `<option value="${esc(f.name)}">${esc(f.name)}</option>`)).join("");
    $("tp-font").value = fonts.some((f) => f.name === cursel) || cursel === "Helvetica" ? cursel : "Helvetica";
  }
  function renderFonts() {
    $("t-fonts").innerHTML = fonts.length
      ? fonts.map((f) => `<div class="frow"><code>${esc(f.name)}</code><button type="button" class="btn sm ghost" data-font-del="${esc(f.name)}">✕</button></div>`).join("")
      : `<span class="faint">Helvetica only — upload a .ttf/.otf to use the brand face.</span>`;
    $("t-fonts").querySelectorAll("[data-font-del]").forEach((b) => b.addEventListener("click", () => {
      const name = b.dataset.fontDel;
      fonts = fonts.filter((f) => f.name !== name);
      items.forEach((it) => { if (it.font === name) it.font = "Helvetica"; });
      renderFonts(); renderFontOptions(); render();
    }));
    renderFontOptions();
  }
  $("t-font-file").addEventListener("change", () => {
    for (const file of $("t-font-file").files || []) {
      if (!/\.(ttf|otf)$/i.test(file.name)) { setStatus(`${file.name}: fonts must be .ttf or .otf.`); continue; }
      if (file.size > 2 * 1024 * 1024) { setStatus(`${file.name} is over 2 MB.`); continue; }
      if (fonts.length >= 3) { setStatus("Three fonts is the limit."); break; }
      if (fonts.some((f) => f.name === file.name)) continue;
      fonts.push({ name: file.name, file });
    }
    $("t-font-file").value = ""; renderFonts(); schedulePreview(0);
  });

  /* ---- snapping: other boxes' edges and centres, and the page's own ----
     A designed page is a grid the designer drew by eye; ±6 px of magnetism is
     what makes "line this up with that" a drag instead of arithmetic. */
  const SNAP_PX = 6;
  function snapEdges(it, edges, axis) {
    // edges: [[value, offsetFromBoxOrigin], …] — every edge that may snap.
    const r = layer.getBoundingClientRect();
    const tol = SNAP_PX / (axis === "x" ? r.width : r.height);
    const targets = [0, 0.5, 1];
    for (const o of visible()) {
      if (o === it) continue;
      const p = axis === "x" ? o.x : o.y, s = axis === "x" ? o.w : o.h;
      targets.push(p, p + s / 2, p + s);
    }
    let best = null;
    for (const [value, off] of edges) {
      for (const t of targets) {
        const d = Math.abs(value - t);
        if (d <= tol && (!best || d < best.d)) best = { d, at: t - off, line: t };
      }
    }
    return best;
  }

  /* ---- pointer: draw new slot on empty space, move / resize items ---- */
  let drag = null;
  layer.addEventListener("pointerdown", (e) => {
    if (!pages[cur]) return;
    // The drag preventDefaults, so focus would stay wherever it was — in the
    // style-name field right after "Make my own version" — and every keyboard
    // shortcut below would silently do nothing on the box you just clicked.
    const active = document.activeElement;
    if (active && /^(input|textarea|select)$/i.test(active.tagName)) active.blur();
    const itemEl = e.target.closest(".titem");
    const r = layer.getBoundingClientRect();
    if (itemEl) {
      const it = items.find((x) => x.id === +itemEl.dataset.id); select(it);
      drag = { mode: e.target.classList.contains("h") ? "resize" : "move", it, sx: e.clientX, sy: e.clientY, ox: it.x, oy: it.y, ow: it.w, oh: it.h, r };
    } else {
      if (cur !== "post") return;
      drag = { mode: "draw", sx: e.clientX, sy: e.clientY, r };
      select(null);
    }
    layer.setPointerCapture(e.pointerId); e.preventDefault();
  });
  layer.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = (e.clientX - drag.sx) / drag.r.width, dy = (e.clientY - drag.sy) / drag.r.height;
    if (drag.mode === "move") {
      const it = drag.it, guides = [];
      let x = Math.min(1 - it.w, Math.max(0, drag.ox + dx));
      let y = Math.min(1 - it.h, Math.max(0, drag.oy + dy));
      const sx = snapEdges(it, [[x, 0], [x + it.w / 2, it.w / 2], [x + it.w, it.w]], "x");
      const sy = snapEdges(it, [[y, 0], [y + it.h / 2, it.h / 2], [y + it.h, it.h]], "y");
      if (sx) { x = Math.min(1 - it.w, Math.max(0, sx.at)); guides.push(["v", sx.line]); }
      if (sy) { y = Math.min(1 - it.h, Math.max(0, sy.at)); guides.push(["h", sy.line]); }
      it.x = x; it.y = y; render(guides); select(it);
    } else if (drag.mode === "resize") {
      const it = drag.it, guides = [];
      let w = Math.min(1 - it.x, Math.max(0.02, drag.ow + dx));
      let h = Math.min(1 - it.y, Math.max(0.02, drag.oh + dy));
      const sx = snapEdges(it, [[it.x + w, it.x]], "x");
      const sy = snapEdges(it, [[it.y + h, it.y]], "y");
      if (sx && sx.at >= 0.02) { w = Math.min(1 - it.x, sx.at); guides.push(["v", sx.line]); }
      if (sy && sy.at >= 0.02) { h = Math.min(1 - it.y, sy.at); guides.push(["h", sy.line]); }
      it.w = w; it.h = h; render(guides); select(it);
    } else if (drag.mode === "draw") {
      let ghost = layer.querySelector(".ghost");
      if (!ghost) { ghost = document.createElement("div"); ghost.className = "titem slot ghost"; layer.append(ghost); }
      const b = rectFrac(drag.sx, drag.sy, e.clientX, e.clientY);
      ghost.style.left = `${b.x * 100}%`; ghost.style.top = `${b.y * 100}%`; ghost.style.width = `${b.w * 100}%`; ghost.style.height = `${b.h * 100}%`;
    }
  });
  layer.addEventListener("pointerup", (e) => {
    if (!drag) return;
    if (drag.mode === "draw") {
      const b = rectFrac(drag.sx, drag.sy, e.clientX, e.clientY);
      layer.querySelector(".ghost")?.remove();
      if (b.w > 0.04 && b.h > 0.04) add("slot", null, b);
    }
    drag = null;
  });

  /* ---- the meta: ONE object, used by save, preview and the Canva guide ---- */
  const box = (it) => ({ x: +it.x.toFixed(4), y: +it.y.toFixed(4), w: +it.w.toFixed(4), h: +it.h.toFixed(4) });
  function buildMeta() {
    const summary = items.find((it) => it.kind === "summary");
    const meta = {
      label: $("t-label").value.trim(), slug: editingSlug || "",
      base: $("t-base").value, paper: $("t-paper").value, orientation: $("t-orient").value,
      // A designed page renders as an exact PDF and an editable PPTX; there
      // is no trailing links page any more, so nothing to toggle here.
      links_table: false, outputs: ["pdf", "pptx"],
      slots: items.filter((it) => it.kind === "slot").map(box),
      logos: items.filter((it) => it.kind === "logo").map(box),
      summary_box: summary ? box(summary) : null,
      fonts: fonts.map((f) => f.name),
      text: items.filter((it) => it.kind === "text").map((it) => Object.assign(
        { field: it.field, size_pt: it.size, color: it.color, align: it.align, page: it.page, bold: !!it.bold }, box(it),
        it.font && it.font !== "Helvetica" ? { font: it.font } : {})),
    };
    if (copyFrom) meta.copy_from = copyFrom;    // borrow the art we did not replace
    return meta;
  }
  const attachFiles = (fd) => {
    for (const k of PAGE_KINDS) if (pages[k] && pages[k].file) fd.append(k, pages[k].file, `${k}.png`);
    for (const f of fonts) if (f.file) fd.append("fonts", f.file, f.name);
  };

  /* ---- live preview: ONE page, drawn by the server exactly as the PDF ---- */
  const pv = $("t-pv"), pvImg = $("t-pv-img"), pvEmpty = $("t-pv-empty"), pvMsg = $("t-pv-msg");
  let previewOn = false, pvTimer = null, pvSeq = 0;
  function schedulePreview(delay) {
    if (!previewOn) return;
    clearTimeout(pvTimer);
    pvTimer = setTimeout(runPreview, delay === undefined ? 800 : delay);
  }
  async function runPreview() {
    if (!previewOn) return;
    if (!items.some((it) => it.kind === "slot")) {
      pvImg.hidden = true; pvEmpty.hidden = false;
      pvEmpty.textContent = "Add a screenshot slot to preview the page.";
      return;
    }
    const seq = ++pvSeq;
    pv.classList.add("busy");
    const fd = new FormData();
    fd.append("csrf_token", CSRF()); fd.append("meta", JSON.stringify(buildMeta())); fd.append("page", cur);
    attachFiles(fd);
    try {
      const res = await fetch("/api/styles/preview-page", { method: "POST", body: fd, headers: { "X-CSRF-Token": CSRF() } });
      if (seq !== pvSeq) return;
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `Preview failed (${res.status})`); }
      const url = URL.createObjectURL(await res.blob());
      if (pvImg.src.startsWith("blob:")) URL.revokeObjectURL(pvImg.src);
      pvImg.src = url; pvImg.hidden = false; pvEmpty.hidden = true;
      pvMsg.textContent = `${cur} page · sample data · one fixture screenshot`;
      pvMsg.style.color = "";
    } catch (err) {
      if (seq !== pvSeq) return;
      pvMsg.textContent = err.message; pvMsg.style.color = "var(--bad)";
    } finally { if (seq === pvSeq) pv.classList.remove("busy"); }
  }
  $("t-preview-btn").addEventListener("click", () => {
    previewOn = !previewOn;
    $("t-preview-btn").setAttribute("aria-pressed", String(previewOn));
    $("t-preview-btn").classList.toggle("primary", previewOn);
    pv.hidden = !previewOn;
    document.querySelector(".tstage").classList.toggle("split", previewOn);
    if (previewOn) runPreview();
  });

  /* ---- the Canva guide: a transparent layer at the page's real pixel size ---- */
  $("t-guide").addEventListener("click", async () => {
    const meta = buildMeta();
    if (!(meta.slots.length || meta.logos.length || meta.text.length))
      return setStatus("Place some slots first — the guide is drawn from them. 'Place standard slots' is the quickest start.");
    const btn = $("t-guide"); btn.disabled = true;
    try {
      const qs = `/api/styles/guide?page=${encodeURIComponent(cur)}&meta=${encodeURIComponent(JSON.stringify(meta))}`;
      const res = qs.length < 6000
        ? await fetch(qs)
        : await fetch("/api/styles/guide", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF() },
          body: JSON.stringify({ csrf_token: CSRF(), meta, page: cur }) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `Could not draw the guide (${res.status})`); }
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      const [gw, gh] = guidePixels();
      a.href = url; a.download = `slot-guide-${(meta.slug || meta.label || "style").replace(/[^a-z0-9-]+/gi, "-").toLowerCase()}-${cur}-${gw}x${gh}.png`;
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      setStatus(`Guide downloaded (${gw}×${gh}px). In Canva: new ${$("t-paper").value} design → upload it as a top layer → design underneath → delete the layer → download PNG.`);
    } catch (err) { setStatus(err.message); }
    finally { btn.disabled = false; }
  });

  /* ---- save ---- */
  const saveBtn = $("t-save"), msg = $("t-msg");
  function setSaveable() {
    const ok = (!!pages.post || !!copyFrom) && items.some((it) => it.kind === "slot") && $("t-label").value.trim();
    saveBtn.disabled = !ok;
  }
  $("t-label").addEventListener("input", setSaveable);
  ["t-paper", "t-orient", "t-base"].forEach((id) => $(id).addEventListener("change", () => { syncKitNote(); schedulePreview(0); }));
  saveBtn.addEventListener("click", async () => {
    msg.textContent = ""; saveBtn.disabled = true;
    const meta = buildMeta();
    for (const k of ["cover", "summary", "end"]) if (!pages[k]) meta[`remove_${k}`] = true;
    const fd = new FormData();
    fd.append("csrf_token", CSRF()); fd.append("meta", JSON.stringify(meta));
    fd.append("overwrite", ($("t-overwrite").checked || !!editingSlug) ? "1" : "");
    attachFiles(fd);
    try {
      const res = await fetch("/api/styles/template", { method: "POST", body: fd, headers: { "X-CSRF-Token": CSRF() } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.detail || `Save failed (${res.status})`);
      msg.innerHTML = `<span class="alert alert-ok tight">Saved <b>${esc(data.label)}</b> — pending until an admin approves it for New report.</span>`;
      setTimeout(() => location.assign(`/styles#style-${encodeURIComponent(data.slug)}`), 500);
      setTimeout(() => location.reload(), 600);
    } catch (err) { msg.innerHTML = `<span class="alert alert-error tight">${esc(err.message)}</span>`; saveBtn.disabled = false; }
  });

  /* ---- edit an existing template style, or start a copy of one ----
     `asCopy` is "Make my own version": same slots, logos, summary box and text,
     the source's page art shown greyed as a placeholder, no name yet. The art
     is BORROWED — `copy_from` in the meta tells the server to keep whichever
     pages the designer did not replace, so a version that only changes the
     wording still saves a complete style. */
  async function loadTemplate(slug, asCopy) {
    try {
      const r = await api(`/api/styles/${encodeURIComponent(slug)}`);
      const p = r.raw, tpl = p.template; if (!tpl) return false;
      editingSlug = asCopy ? "" : slug;
      copyFrom = asCopy ? slug : "";
      $("t-label").value = asCopy ? "" : p.label; $("t-base").value = p.extends || "twitter";
      $("t-paper").value = String((p.page || {}).size || "a4").toLowerCase(); $("t-orient").value = (p.page || {}).orientation || "portrait";
      // Duplicating a SHIPPED template (e.g. combined-16x9): it has no `extends`, so keep its own engine
      if (!p.extends && p.capture && p.capture.engine) $("t-base").value = { combined: "combined-16x9", instagram: "instagram", facebook: "facebook", influencer: "influencer", x: "twitter" }[p.capture.engine] || "twitter";
      items = []; nextId = 1;
      fonts = (tpl.fonts || []).map((name) => ({ name, file: null }));
      for (const k of PAGE_KINDS) {
        pages[k] = null;
        if ((tpl.pages || {})[k]) {
          const url = `/api/styles/${encodeURIComponent(slug)}/asset/${k}`;
          await new Promise((res) => { const pr = new Image(); pr.onload = () => { pages[k] = { file: null, url, w: pr.naturalWidth, h: pr.naturalHeight, ghost: !!asCopy }; res(); }; pr.onerror = res; pr.src = url; });
        }
      }
      (tpl.slots || []).forEach((s) => items.push({ id: nextId++, kind: "slot", page: "post", ...s }));
      (tpl.logos || []).forEach((s) => items.push({ id: nextId++, kind: "logo", page: "post", ...s }));
      if (tpl.summary_box) items.push({ id: nextId++, kind: "summary", page: "summary", ...tpl.summary_box });
      (tpl.text || []).forEach((t) => items.push({ id: nextId++, kind: "text", page: t.page || "post", field: t.field, x: t.x, y: t.y, w: t.w, h: t.h, size: t.size_pt || 10, color: t.color || "#111111", align: t.align || "left", bold: !!t.bold, font: t.font || "Helvetica" }));
      $("t-banner").hidden = !asCopy;
      $("t-banner-src").textContent = asCopy ? `Everything from “${p.label}” is here — slots, logo, summary box and text.` : "";
      renderFonts(); syncKitNote(); showPage("post");
      setStatus(asCopy ? `Copied the slots from ${p.label}. The greyed art is a placeholder — drop your own page PNG on each tab, name the style, Save.`
        : `Editing ${p.label}. Existing page images are kept unless you upload new ones.`);
      if (asCopy) $("t-label").focus();
      $("tdesigner").scrollIntoView({ behavior: "smooth" });
      return true;
    } catch (_) { return false; }
  }
  document.querySelectorAll("[data-edit][data-tpl='1']").forEach((b) => b.addEventListener("click", () => loadTemplate(b.dataset.edit, false)));
  document.querySelectorAll("[data-mine]").forEach((b) => b.addEventListener("click", () => loadTemplate(b.dataset.mine, true)));

  renderFonts(); syncKitNote(); showPage("post"); setSaveable();
  const h = location.hash;
  if (h.startsWith("#mine=")) loadTemplate(decodeURIComponent(h.slice(6)), true);
}

/* =========================================================================
   v3 · Project → Styles: tick styles from the pool, per-style files, background
   ========================================================================= */
function initProjectStyles() {
  const grid = $("pick-grid"); if (!grid) return;
  const pid = grid.dataset.pid;
  const cards = [...grid.querySelectorAll(".pick[data-slug]")];
  const status = $("ps-status"), count = $("ps-count");
  let project = {}; try { project = JSON.parse($("pstyles-data").textContent) || {}; } catch (_) {}
  const say = (t, cls) => { status.textContent = t; status.style.color = cls === "bad" ? "var(--bad)" : cls === "ok" ? "var(--ok)" : "var(--text3)"; };

  /* what the server knows about each picked style's background */
  const bgOf = (slug) => ((project.styles || []).find((s) => s.slug === slug) || {}).background || {};
  const paintBg = (c) => {
    const sw = c.querySelector("[data-bgsw]"), tx = c.querySelector("[data-bgtext]"); if (!sw) return;
    const bg = bgOf(c.dataset.slug);
    sw.style.background = bg.color || "#fff";
    sw.style.backgroundImage = bg.image ? "repeating-linear-gradient(45deg,#cbd5e1 0 4px,#fff 4px 8px)" : "";
    tx.textContent = bg.image ? (bg.color ? `Image + ${bg.color}` : "Image") : (bg.color ? bg.color : "No background");
  };
  const refresh = () => {
    cards.forEach((c) => {
      const on = c.classList.contains("on");
      c.setAttribute("aria-checked", String(on));
      const row = c.querySelector("[data-bgrow]"); if (row) row.hidden = !on;
      c.querySelectorAll("[data-outs] label").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked));
      paintBg(c);
    });
    const n = cards.filter((c) => c.classList.contains("on")).length;
    count.textContent = `${n} picked · ${cards.filter((c) => !c.hidden).length} shown`;
    const k = document.querySelector('.nav a[href="/project/styles"] .k'); if (k) k.textContent = String(n);
  };
  const payload = () => cards.filter((c) => c.classList.contains("on")).map((c) => ({
    slug: c.dataset.slug,
    outputs: [...c.querySelectorAll("[data-outs] input:checked")].map((i) => i.value),
  }));
  let timer = null;
  const save = () => {
    clearTimeout(timer);
    say("Saving…");
    timer = setTimeout(async () => {
      try {
        const r = await api(`/api/projects/${encodeURIComponent(pid)}/styles`, { method: "PUT", json: { styles: payload() } });
        project = r.project || project; say("Saved", "ok"); refresh();
      } catch (err) { say(err.message, "bad"); }
    }, 350);
  };
  cards.forEach((c) => {
    c.addEventListener("click", (e) => {
      if (e.target.closest("[data-outs]") || e.target.closest("[data-bg]")) return;
      c.classList.toggle("on");
      // Ticking with nothing chosen means every file the style builds.
      if (c.classList.contains("on") && !c.querySelectorAll("[data-outs] input:checked").length) c.querySelectorAll("[data-outs] input").forEach((i) => { i.checked = true; });
      refresh(); save();
    });
    c.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); c.click(); } });
    c.querySelectorAll("[data-outs] input").forEach((i) => i.addEventListener("change", () => {
      if (!c.classList.contains("on")) c.classList.add("on");
      if (![...c.querySelectorAll("[data-outs] input")].some((x) => x.checked)) { i.checked = true; say("Keep at least one file for a picked style.", "bad"); }
      refresh(); save();
    }));
  });

  /* filters */
  const fPlat = $("pf-plat"), fKind = $("pf-kind"), fPicked = $("pf-picked");
  const filter = () => {
    fPicked.classList.toggle("on", fPicked.querySelector("input").checked);
    cards.forEach((c) => {
      const ok = (!fPlat.value || c.dataset.platform === fPlat.value)
        && (!fKind.value || c.dataset.kind === fKind.value)
        && (!fPicked.querySelector("input").checked || c.classList.contains("on"));
      c.hidden = !ok;
    });
    refresh();
  };
  [fPlat, fKind].forEach((el) => el.addEventListener("change", filter));
  fPicked.querySelector("input").addEventListener("change", filter);

  /* background dialog */
  const modal = $("bg-modal"), form = $("bg-form"), msg = $("bg-msg"), colorIn = $("bg-color"), hex = $("bg-hex"), fileIn = $("bg-file"), fileName = $("bg-file-name");
  let bgSlug = "";
  const setColor = (c) => { colorIn.value = c; hex.textContent = c; $("bg-swatches").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.color.toUpperCase() === c.toUpperCase())); };
  const openBg = (slug, label) => {
    bgSlug = slug; $("bg-title").textContent = label; msg.textContent = ""; fileIn.value = ""; fileName.textContent = "";
    setColor((bgOf(slug).color || "#FFFFFF").toUpperCase());
    modal.hidden = false;
  };
  const closeBg = () => { modal.hidden = true; };
  grid.addEventListener("click", (e) => {
    const b = e.target.closest("[data-bg]"); if (!b) return;
    e.stopPropagation();
    const c = b.closest(".pick");
    if (!c.classList.contains("on")) { c.classList.add("on"); c.querySelectorAll("[data-outs] input").forEach((i) => { i.checked = true; }); refresh(); }
    // The style must be in the project before it can take a background.
    api(`/api/projects/${encodeURIComponent(pid)}/styles`, { method: "PUT", json: { styles: payload() } })
      .then((r) => { project = r.project || project; openBg(c.dataset.slug, c.dataset.label); })
      .catch((err) => say(err.message, "bad"));
  });
  $("bg-close").addEventListener("click", closeBg);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeBg(); });
  $("bg-swatches").addEventListener("click", (e) => { const b = e.target.closest("[data-color]"); if (b) setColor(b.dataset.color); });
  colorIn.addEventListener("input", () => setColor(colorIn.value.toUpperCase()));
  fileIn.addEventListener("change", () => { fileName.textContent = fileIn.files[0] ? `${fileIn.files[0].name} · ${(fileIn.files[0].size / 1024).toFixed(0)} KB` : ""; });
  const sendBg = async (remove) => {
    msg.textContent = "Applying…"; msg.style.color = "";
    const body = new FormData();
    body.append("csrf_token", CSRF());
    if (remove) body.append("remove", "1"); else {
      body.append("color", colorIn.value.toUpperCase());
      if (fileIn.files[0]) body.append("image", fileIn.files[0]);
    }
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(pid)}/styles/${encodeURIComponent(bgSlug)}/background`, { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.detail || `Failed (${res.status})`);
      msg.textContent = data.slug !== bgSlug ? "Applied — a copy of the style now belongs to this project. Reloading…" : "Applied. Reloading…";
      msg.style.color = "var(--ok)";
      setTimeout(() => location.reload(), 700);      // thumbnails re-render server-side
    } catch (err) { msg.textContent = err.message; msg.style.color = "var(--bad)"; }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); sendBg(false); });
  $("bg-remove").addEventListener("click", () => sendBg(true));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeBg(); });

  refresh();
}

/* =========================================================================
   v3 · Project → Settings
   ========================================================================= */
function initProjectSettings() {
  const form = $("proj-form"); if (!form) return;
  const pid = form.dataset.pid, msg = $("proj-msg");
  const say = (t, ok) => { msg.textContent = t; msg.style.color = ok ? "var(--ok)" : "var(--bad)"; };
  form.addEventListener("submit", async (e) => {
    e.preventDefault(); say("Saving…", true);
    const f = form.elements;
    try {
      await api(`/api/projects/${encodeURIComponent(pid)}`, { method: "PATCH", json: {
        name: f.name.value.trim(), client: f.client.value.trim(), emoji: f.emoji.value.trim(),
        settings: { dedupe: f.dedupe.checked, keep_engagement: f.keep_engagement.checked,
          workers: +f.workers.value || 0, note: f.note.value.trim() } } });
      say("Saved.", true); setTimeout(() => location.reload(), 500);
    } catch (err) { say(err.message, false); }
  });
  const arch = $("proj-archive");
  if (arch) arch.addEventListener("click", async () => {
    if (!confirm(`Archive "${form.elements.name.value}"? Its runs stay downloadable from their links; the project leaves the dropdown.`)) return;
    try { await api(`/api/projects/${encodeURIComponent(pid)}`, { method: "DELETE" }); location.href = "/"; }
    catch (err) { say(err.message, false); }
  });
}
