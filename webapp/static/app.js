/* Report Automation — submit form + job status polling. No build step, no deps. */

function initSubmitForm() {
  const $ = (id) => document.getElementById(id);
  const form = $("job-form");
  const drop = $("drop");
  const input = $("file-input");
  const paste = $("paste-input");
  const dedupe = $("dedupe");
  const chip = $("file-chip");
  const chipName = $("file-name");
  const clearBtn = $("file-clear");
  const nameInput = $("report-name");
  const errorBox = $("form-error");
  const submitBtn = $("submit-btn");
  const submitHint = $("submit-hint");
  const spinner = submitBtn.querySelector(".spinner");
  const cropOption = $("crop-option");
  const keepEngagement = $("keep-engagement");
  const speedOption = $("speed-option");
  const workersSelect = $("workers");

  const pv = {
    count: $("preview-count"), empty: $("preview-empty"),
    loading: $("preview-loading"), body: $("preview-body"),
    rows: $("preview-rows"), more: $("preview-more"),
    warn: $("preview-warnings"), error: $("preview-error"),
  };

  let activeTab = "file";
  let ready = 0; // link count the preview last confirmed

  // ---------- capability-driven options ----------
  // The form no longer knows what "twitter" means. Each report type carries
  // flags, so a new profile gets the right controls with no JS change.
  const selectedRadio = () =>
    form.querySelector('input[name="report_type"]:checked');
  const syncOptions = () => {
    const r = selectedRadio();
    if (!r) return;
    if (cropOption) cropOption.hidden = r.dataset.keepEngagement !== "1";
    if (speedOption) speedOption.hidden = r.dataset.workerChoice !== "1";
  };
  form.querySelectorAll('input[name="report_type"]').forEach((radio) =>
    radio.addEventListener("change", syncOptions)
  );
  syncOptions();

  // ---------- tabs ----------
  const tabs = [...form.querySelectorAll(".tab")];
  const selectTab = (name) => {
    activeTab = name;
    tabs.forEach((t) => {
      const on = t.dataset.tab === name;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
    });
    form.querySelectorAll(".tabpanel").forEach((p) => {
      p.hidden = p.dataset.panel !== name;
    });
    schedulePreview(0);
  };
  tabs.forEach((t) => {
    t.addEventListener("click", () => selectTab(t.dataset.tab));
    t.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(t);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.focus();
        selectTab(next.dataset.tab);
      }
    });
  });

  const showError = (msg) => {
    errorBox.textContent = msg || "";
    errorBox.hidden = !msg;
  };

  // ---------- file chip ----------
  const showFile = (file) => {
    chip.hidden = !file;
    if (!file) return;
    chipName.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
    showError("");
    if (!nameInput.value.trim()) {
      nameInput.value = file.name.replace(/\.[^.]+$/, "").slice(0, 80);
    }
  };

  input.addEventListener("change", () => {
    showFile(input.files[0]);
    schedulePreview(0);
  });
  clearBtn.addEventListener("click", () => {
    input.value = "";
    showFile(null);
    schedulePreview(0);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    })
  );
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    showFile(file);
    schedulePreview(0);
  });

  paste.addEventListener("input", () => schedulePreview(450));
  dedupe.addEventListener("change", () => schedulePreview(0));

  // ---------- preview ----------
  const setReady = (n) => {
    ready = n;
    submitBtn.disabled = n <= 0;
    submitHint.textContent = n > 0
      ? `${n} link${n === 1 ? "" : "s"} ready to capture.`
      : "Add some links to continue.";
  };

  const resetPreview = () => {
    pv.body.hidden = true;
    pv.loading.hidden = true;
    pv.error.hidden = true;
    pv.empty.hidden = false;
    pv.count.hidden = true;
    setReady(0);
  };

  const note = (cls, html) => {
    const d = document.createElement("div");
    d.className = `alert ${cls} tight`;
    d.innerHTML = html;
    return d;
  };

  const renderPreview = (data) => {
    pv.loading.hidden = true;
    pv.empty.hidden = true;
    pv.error.hidden = true;
    pv.body.hidden = false;
    pv.count.hidden = false;
    pv.count.textContent = data.count;

    pv.warn.innerHTML = "";
    if (data.over_limit) {
      pv.warn.append(note("alert-error",
        `<strong>${data.count} links</strong> — the limit is ${data.limit} per report. Split this into smaller batches.`));
    }
    if (data.duplicate_count) {
      pv.warn.append(note("alert-warn", data.dedupe_applied
        ? `<strong>${data.duplicate_count}</strong> duplicate post(s) removed.`
        : `<strong>${data.duplicate_count}</strong> duplicate post(s) found — tick “Remove duplicate posts” to drop them.`));
    }
    if (data.dropped_count) {
      const list = data.dropped.slice(0, 5)
        .map((d) => `<li>row ${d.row}: <code>${escapeHtml(d.value)}</code> — ${escapeHtml(d.reason)}</li>`)
        .join("");
      const more = data.dropped_count > 5
        ? `<li>…and ${data.dropped_count - 5} more</li>` : "";
      pv.warn.append(note("alert-warn",
        `<strong>${data.dropped_count}</strong> line(s) skipped:<ul class="drop-list">${list}${more}</ul>`));
    }

    pv.rows.innerHTML = "";
    for (const r of data.rows.slice(0, 60)) {
      const li = document.createElement("li");
      const who = document.createElement("strong");
      who.textContent = r.account || "X post";
      const a = document.createElement("a");
      a.href = r.link;
      a.textContent = r.link;
      a.target = "_blank";
      a.rel = "noopener";
      li.append(who, a);
      pv.rows.append(li);
    }
    pv.more.hidden = data.rows.length <= 60;
    pv.more.textContent = data.rows.length > 60
      ? `…and ${data.count - 60} more` : "";

    setReady(data.over_limit ? 0 : data.count);
  };

  const showPreviewError = (msg, dropped) => {
    pv.loading.hidden = true;
    pv.body.hidden = true;
    pv.empty.hidden = true;
    pv.count.hidden = true;
    let html = escapeHtml(msg);
    if (dropped && dropped.length) {
      html += `<ul class="drop-list">${dropped.slice(0, 5)
        .map((d) => `<li>row ${d.row}: <code>${escapeHtml(d.value)}</code></li>`)
        .join("")}</ul>`;
    }
    pv.error.innerHTML = html;
    pv.error.hidden = false;
    setReady(0);
  };

  let previewTimer = null;
  let previewSeq = 0;

  function schedulePreview(delay) {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, delay);
  }

  async function runPreview() {
    const body = new FormData();
    body.append("csrf_token", form.csrf_token.value);
    if (dedupe.checked) body.append("dedupe", "1");

    if (activeTab === "file") {
      if (!input.files.length) return resetPreview();
      body.append("file", input.files[0]);
    } else {
      if (!paste.value.trim()) return resetPreview();
      body.append("text", paste.value);
    }

    const seq = ++previewSeq;
    pv.empty.hidden = true;
    pv.error.hidden = true;
    pv.body.hidden = true;
    pv.loading.hidden = false;

    try {
      const res = await fetch("/api/preview", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (seq !== previewSeq) return; // a newer request already went out
      if (!res.ok || !data.ok) {
        return showPreviewError(data.detail || `Could not read that (${res.status})`,
                                data.dropped);
      }
      renderPreview(data);
    } catch (_) {
      if (seq === previewSeq) showPreviewError("Preview unavailable — check your connection.");
    }
  }

  // ---------- submit ----------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");
    if (!ready) return showError("Add some links first.");
    if (!nameInput.value.trim()) {
      nameInput.focus();
      return showError("Give the report a name.");
    }

    submitBtn.disabled = true;
    spinner.hidden = false;

    const body = new FormData();
    body.append("report_name", nameInput.value.trim());
    body.append("report_type", form.report_type.value);
    body.append("csrf_token", form.csrf_token.value);
    if (dedupe.checked) body.append("dedupe", "1");
    if (activeTab === "file") body.append("file", input.files[0]);
    else body.append("text", paste.value);

    const r = selectedRadio();
    if (keepEngagement?.checked && r?.dataset.keepEngagement === "1") {
      body.append("keep_engagement", "1");
    }
    if (workersSelect?.value && r?.dataset.workerChoice === "1") {
      body.append("workers", workersSelect.value);
    }

    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
      window.location.href = `/jobs/${data.job_id}`;
    } catch (err) {
      showError(err.message);
      submitBtn.disabled = false;
      spinner.hidden = true;
    }
  });

  resetPreview();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function initJobPage(executionMode) {
  const card = document.getElementById("status-card");
  const jobId = card.dataset.jobId;
  const csrf = card.dataset.csrf;

  const pill = document.getElementById("status-pill");
  const phase = document.getElementById("phase");
  const wrap = document.getElementById("progress-wrap");
  const bar = document.getElementById("progress-bar");
  const counter = document.getElementById("counter");
  const elapsed = document.getElementById("elapsed");
  const errBox = document.getElementById("job-error");
  const downloads = document.getElementById("downloads");
  const cancelForm = document.getElementById("cancel-form");
  const activity = document.getElementById("activity");
  const skippedCard = document.getElementById("skipped-card");
  const skippedList = document.getElementById("skipped");
  const skippedCount = document.getElementById("skipped-count");
  const ephemeralNote = document.getElementById("ephemeral-note");

  const fmtTime = (s) => {
    const m = Math.floor(s / 60);
    return m ? `${m}m ${s % 60}s` : `${s}s`;
  };

  const renderActivity = (items) => {
    if (!items.length) return;
    activity.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      li.className = item.level || "info";
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = new Date(item.t * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      li.append(ts, document.createTextNode(item.message));
      activity.append(li);
    }
  };

  const renderSkipped = (items) => {
    if (!items.length) {
      skippedCard.hidden = true;
      return;
    }
    skippedCard.hidden = false;
    skippedCount.textContent = items.length;
    skippedList.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      const head = document.createElement("strong");
      head.textContent = item.account || item.link || "Unknown post";
      const why = document.createElement("span");
      why.className = "why";
      why.textContent = `${item.reason}${item.account && item.link ? " — " + item.link : ""}`;
      li.append(head, why);
      skippedList.append(li);
    }
  };

  const render = (job) => {
    pill.textContent = job.status;
    pill.className = `status status-${job.status}`;
    phase.textContent = job.phase || "";
    elapsed.textContent = job.elapsed ? fmtTime(job.elapsed) : "";

    const running = job.status === "running" || job.status === "queued";
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    wrap.classList.toggle("indeterminate", running && !job.done);
    wrap.hidden = job.status === "queued" && !job.total;
    bar.style.width = `${job.status === "done" ? 100 : pct}%`;
    counter.textContent = job.total
      ? `${Math.min(job.done, job.total)} / ${job.total} posts captured`
      : "";

    errBox.hidden = !job.error;
    errBox.textContent = job.error || "";

    renderActivity(job.activity || []);
    renderSkipped(job.skipped || []);

    const has = (k) => (job.artifacts || []).includes(k);
    const hasAny = (job.artifacts || []).length > 0;
    downloads.hidden = !hasAny;
    for (const kind of ["pdf", "docx", "zip"]) {
      const el = document.getElementById(`dl-${kind}`);
      el.hidden = !has(kind);
      if (has(kind)) el.href = `/api/jobs/${jobId}/download/${kind}`;
    }
    // Scale-to-zero hosts throw the files away when the instance stops.
    ephemeralNote.hidden = !(hasAny && job.execution_mode === "inline");

    cancelForm.hidden = job.finished;
    document.title = `${job.status} · ${job.name} — Report Automation`;
    return job.finished;
  };

  cancelForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new FormData();
    body.append("csrf_token", csrf);
    const res = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST", body });
    if (res.ok) render(await res.json());
  });

  let delay = 1500;
  const poll = async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (res.status === 401) return (window.location.href = "/login");
      if (!res.ok) throw new Error("status unavailable");
      if (render(await res.json())) return; // terminal state — stop polling
      delay = Math.min(delay * 1.15, 5000); // ease off on long jobs
    } catch (_) {
      delay = Math.min(delay * 2, 15000); // server restarting? back off
    }
    setTimeout(poll, delay);
  };

  // Inline mode (scale-to-zero hosts): the container is frozen once a response
  // is sent, so the capture runs inside a request we hold open. That same
  // response streams NDJSON status, which we render directly — polling would be
  // unreliable here because another auto-scaled instance may not know this job.
  const runInline = async () => {
    const res = await fetch(`/api/jobs/${jobId}/run-inline`);
    if (res.status === 409) return poll(); // already running (e.g. page reload)
    if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // keep the partial line for the next chunk
      for (const line of lines) {
        if (!line.trim()) continue; // keep-alive
        try {
          render(JSON.parse(line));
        } catch (_) {
          /* ignore a malformed frame rather than kill the stream */
        }
      }
    }
  };

  if (executionMode === "inline") {
    runInline().catch(() => poll()); // network hiccup — fall back to polling
  } else {
    poll();
  }
}
