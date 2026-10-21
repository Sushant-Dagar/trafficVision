"use strict";

/* ------------------------------------------------------------------ *
 * TrafficVision - frontend logic
 * Single-page: gallery <-> detail, hash-routed (#/  and  #/video/<id>)
 * ------------------------------------------------------------------ */

const el = (sel) => document.querySelector(sel);

const gallery = el("#gallery");
const emptyState = el("#emptyState");
const jobCount = el("#jobCount");
const viewGallery = el("#view-gallery");
const viewDetail = el("#view-detail");
const detail = el("#detail");
const toasts = el("#toasts");

const fileInput = el("#fileInput");
const dropzone = el("#dropzone");
const urlInput = el("#urlInput");

const selectModeBtn = el("#selectModeBtn");
const bulkBar = el("#bulkBar");
const bulkCount = el("#bulkCount");

let pollTimer = null;
let detailTimer = null;
let selectMode = false;
const selectedIds = new Set();

/* ---------------- Toasts (replaces alert()) ---------------- */

function showToast(message) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = message;
  toasts.appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

/* ---------------- Add from URL ---------------- */

el("#addUrlBtn").addEventListener("click", addUrl);
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") addUrl();
});

async function addUrl() {
  const url = urlInput.value.trim();
  if (!url) return;

  const startRaw = el("#urlTrimStart").value.trim();
  const endRaw = el("#urlTrimEnd").value.trim();
  const start = startRaw === "" ? 0 : parseFloat(startRaw);
  const end = endRaw === "" ? null : parseFloat(endRaw);
  if (isNaN(start) || start < 0 || (end !== null && (isNaN(end) || end <= start))) {
    showToast("Enter a valid trim start/end (end must be after start).");
    return;
  }

  const roadCount = await askRoadCount("This applies to every video from this link.");
  if (roadCount === null) return;

  const body = { url, road_count: roadCount };
  if (start > 0) body.start = start;
  if (end !== null) body.end = end;

  const btn = el("#addUrlBtn");
  btn.disabled = true;
  btn.textContent = "Adding…";
  try {
    const res = await fetch("/api/videos/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Could not add URL: ${err.detail || res.statusText}`);
    } else {
      const data = await res.json();
      urlInput.value = "";
      el("#urlTrimStart").value = "";
      el("#urlTrimEnd").value = "";
      if (data.count > 1) {
        showToast(`Queued ${data.count} videos from playlist.`);
      }
    }
  } catch (e) {
    showToast(`Could not add URL: ${e}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Add from URL";
    refreshGallery();
  }
}

/* ---------------- Upload handling ---------------- */

el("#browseBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.click();
});
dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  uploadFiles([...fileInput.files]);
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (e) => {
  const files = [...(e.dataTransfer?.files || [])].filter((f) =>
    f.type.startsWith("video/") || /\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(f.name)
  );
  uploadFiles(files);
});

async function uploadFiles(files) {
  const startRaw = el("#uploadTrimStart").value.trim();
  const endRaw = el("#uploadTrimEnd").value.trim();
  const start = startRaw === "" ? 0 : parseFloat(startRaw);
  const end = endRaw === "" ? null : parseFloat(endRaw);
  if (isNaN(start) || start < 0 || (end !== null && (isNaN(end) || end <= start))) {
    showToast("Enter a valid trim start/end (end must be after start).");
    return;
  }

  if (!files.length) return;
  const roadCount = await askRoadCount(
    files.length > 1 ? `This applies to all ${files.length} files.` : files[0].name
  );
  if (roadCount === null) return;

  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("road_count", roadCount);
    if (start > 0) fd.append("trim_start", start);
    if (end !== null) fd.append("trim_end", end);
    try {
      const res = await fetch("/api/videos", { method: "POST", body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(`Upload failed for "${file.name}": ${err.detail || res.statusText}`);
      }
    } catch (e) {
      showToast(`Upload failed for "${file.name}": ${e}`);
    }
  }
  refreshGallery();
}

/* ---------------- "How many roads?" dialog ---------------- */

const MAX_ROADS = 8;

/** Ask how many roads meet at the junction. Resolves to a number, or null if cancelled. */
function askRoadCount(subtitle, current) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "road-dialog";
    const options = [];
    for (let n = 2; n <= MAX_ROADS; n++) options.push(n);
    dlg.innerHTML = `
      <form method="dialog">
        <h3>How many roads meet at this junction?</h3>
        <p class="muted">${escapeHtml(subtitle || "")}</p>
        <div class="road-count-grid">
          ${options
            .map(
              (n) => `<button type="button" class="road-count-btn${n === (current || 4) ? " suggested" : ""}" data-n="${n}">
                ${roadCountIcon(n)}<span>${n}</span></button>`
            )
            .join("")}
        </div>
        <p class="hint">Count every road that joins, including one-way roads. You can fix the outlines after the first pass.</p>
        <div class="dialog-actions"><button type="button" class="link" data-cancel>Cancel</button></div>
      </form>`;
    document.body.appendChild(dlg);
    let done = false;
    const finish = (val) => {
      if (done) return;
      done = true;
      dlg.close();
      dlg.remove();
      resolve(val);
    };
    dlg.querySelectorAll("[data-n]").forEach((b) =>
      b.addEventListener("click", () => finish(parseInt(b.dataset.n, 10)))
    );
    dlg.querySelector("[data-cancel]").addEventListener("click", () => finish(null));
    dlg.addEventListener("cancel", (e) => {
      e.preventDefault();
      finish(null);
    });
    dlg.showModal();
    const first = dlg.querySelector(".suggested") || dlg.querySelector("[data-n]");
    first?.focus();
  });
}

/** Tiny star-shaped junction icon with n arms. */
function roadCountIcon(n) {
  const arms = [];
  for (let i = 0; i < n; i++) {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    arms.push(`<line x1="20" y1="20" x2="${(20 + 16 * Math.cos(a)).toFixed(1)}" y2="${(20 + 16 * Math.sin(a)).toFixed(1)}"/>`);
  }
  return `<svg viewBox="0 0 40 40" aria-hidden="true">${arms.join("")}<circle cx="20" cy="20" r="3.5"/></svg>`;
}

/* ---------------- Remove / retry ---------------- */

async function removeJob(jobId, name, status) {
  const isActive = RUNNING.includes(status);
  const prompt = isActive
    ? `Stop and remove "${name}"? It's still ${status} — this cancels it and deletes any partial files.`
    : `Remove "${name}"? This deletes it from the gallery and disk.`;
  if (!confirm(prompt)) return;
  try {
    const res = await fetch(`/api/videos/${jobId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Could not remove: ${err.detail || res.statusText}`);
      return;
    }
    const data = await res.json();
    if (data.status === "cancelling") {
      showToast(`Stopping "${name}"…`);
    }
  } catch (e) {
    showToast(`Could not remove: ${e}`);
    return;
  }
  if (location.hash.includes(`/video/${jobId}`)) {
    location.hash = "#/";
  } else {
    refreshGallery();
  }
}

async function retryJob(jobId, roadCount) {
  try {
    const res = await fetch(`/api/videos/${jobId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(roadCount ? { road_count: roadCount } : {}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Could not retry: ${err.detail || res.statusText}`);
      return;
    }
    showToast("Retrying…");
  } catch (e) {
    showToast(`Could not retry: ${e}`);
    return;
  }
  refreshGallery();
}

async function trimJob(jobId) {
  const startEl = el("#trimStart");
  const endEl = el("#trimEnd");
  const btn = el("#trimBtn");

  const start = parseFloat(startEl.value || "0") || 0;
  const endRaw = endEl.value.trim();
  const end = endRaw === "" ? null : parseFloat(endRaw);

  if (start < 0 || (end !== null && (isNaN(end) || end <= start))) {
    showToast("Enter a valid start/end (end must be after start).");
    return;
  }
  if (end === null && start <= 0) {
    showToast("Enter a start time after 0, or an end time, to trim.");
    return;
  }

  const body = { start };
  if (end !== null) body.end = end;

  btn.disabled = true;
  btn.textContent = "Trimming…";
  try {
    const res = await fetch(`/api/videos/${jobId}/trim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Could not trim: ${err.detail || res.statusText}`);
      btn.disabled = false;
      btn.textContent = "Trim & reprocess";
      return;
    }
    showToast("Trimmed — reprocessing…");
  } catch (e) {
    showToast(`Could not trim: ${e}`);
    btn.disabled = false;
    btn.textContent = "Trim & reprocess";
    return;
  }
  refreshGallery();
  route();
}

/* ---------------- Gallery ---------------- */

const STATUS_LABEL = {
  downloading: "Downloading",
  queued: "Queued",
  processing: "Detecting",
  awaiting_roads: "Mark roads",
  rendering: "Counting per road",
  cancelling: "Cancelling…",
  done: "Done",
  error: "Error",
};

const RUNNING = ["downloading", "processing", "rendering"];
const ACTIVE_STATUSES = ["downloading", "queued", "processing", "rendering", "cancelling"];

async function refreshGallery() {
  let jobs = [];
  try {
    const res = await fetch("/api/videos");
    jobs = await res.json();
  } catch (e) {
    return; // transient; next poll will retry
  }

  jobCount.textContent = jobs.length ? `${jobs.length} video${jobs.length > 1 ? "s" : ""}` : "";
  emptyState.style.display = jobs.length ? "none" : "flex";
  emptyState.style.flexDirection = "column";
  emptyState.style.alignItems = "center";

  // Remove empty-state node from the grid children set when rendering cards.
  gallery.querySelectorAll(".card").forEach((c) => c.remove());

  const anyActive = jobs.some((j) => ACTIVE_STATUSES.includes(j.status));

  for (const job of jobs) {
    gallery.appendChild(renderCard(job));
  }

  // Drop selections for jobs that no longer exist (deleted elsewhere).
  const liveIds = new Set(jobs.map((j) => j.id));
  for (const id of [...selectedIds]) {
    if (!liveIds.has(id)) selectedIds.delete(id);
  }
  updateBulkBar();

  // Poll while anything is still working.
  if (anyActive && !pollTimer) {
    pollTimer = setInterval(refreshGallery, 2500);
  } else if (!anyActive && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function toggleSelect(jobId) {
  if (selectedIds.has(jobId)) selectedIds.delete(jobId);
  else selectedIds.add(jobId);
  refreshGallery();
}

function updateBulkBar() {
  bulkCount.textContent = `${selectedIds.size} selected`;
}

function setSelectMode(on) {
  selectMode = on;
  bulkBar.hidden = !on;
  selectModeBtn.textContent = on ? "Cancel" : "Select";
  if (!on) selectedIds.clear();
  refreshGallery();
}

selectModeBtn.addEventListener("click", () => setSelectMode(!selectMode));

el("#bulkSelectAllBtn").addEventListener("click", async () => {
  let jobsList = [];
  try {
    const res = await fetch("/api/videos");
    jobsList = await res.json();
  } catch (e) {
    return;
  }
  jobsList.forEach((j) => selectedIds.add(j.id));
  refreshGallery();
});

el("#bulkClearBtn").addEventListener("click", () => {
  selectedIds.clear();
  refreshGallery();
});

el("#bulkDoneBtn").addEventListener("click", () => setSelectMode(false));

el("#bulkDeleteBtn").addEventListener("click", async () => {
  if (selectedIds.size === 0) {
    showToast("Select at least one video first.");
    return;
  }
  const ids = [...selectedIds];
  if (!confirm(`Remove ${ids.length} video${ids.length > 1 ? "s" : ""}? Any still downloading/processing will be stopped.`)) return;
  try {
    const res = await fetch("/api/videos/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Bulk delete failed: ${err.detail || res.statusText}`);
      return;
    }
    const data = await res.json();
    const parts = [];
    if (data.deleted?.length) parts.push(`${data.deleted.length} removed`);
    if (data.cancelling?.length) parts.push(`${data.cancelling.length} stopping`);
    showToast(parts.length ? parts.join(", ") : "Nothing to remove.");
  } catch (e) {
    showToast(`Bulk delete failed: ${e}`);
    return;
  }
  selectedIds.clear();
  refreshGallery();
});

el("#bulkRetryBtn").addEventListener("click", async () => {
  if (selectedIds.size === 0) {
    showToast("Select at least one video first.");
    return;
  }
  const ids = [...selectedIds];
  try {
    const res = await fetch("/api/videos/bulk-retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(`Bulk retry failed: ${err.detail || res.statusText}`);
      return;
    }
    const data = await res.json();
    const parts = [];
    if (data.retried?.length) parts.push(`${data.retried.length} queued`);
    if (data.skipped?.length) parts.push(`${data.skipped.length} skipped (not done/error)`);
    showToast(parts.length ? parts.join(", ") : "Nothing to retry.");
  } catch (e) {
    showToast(`Bulk retry failed: ${e}`);
    return;
  }
  selectedIds.clear();
  refreshGallery();
});

function renderCard(job) {
  const card = document.createElement("div");
  const openable = job.status === "done" || job.status === "awaiting_roads";
  card.className = "card" + (openable ? "" : " disabled") + (selectMode ? " selectable" : "");

  if (selectMode) {
    const checkbox = document.createElement("div");
    checkbox.className = "card-checkbox" + (selectedIds.has(job.id) ? " checked" : "");
    checkbox.innerHTML = selectedIds.has(job.id)
      ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>`
      : "";
    card.appendChild(checkbox);
    card.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleSelect(job.id);
    });
    // Fall through to render title/badge/etc. below, but skip the normal
    // click-to-open and remove-button wiring that non-select-mode cards get.
  }

  const canRemove = !selectMode && job.status !== "cancelling";
  if (canRemove) {
    const removeBtn = document.createElement("button");
    removeBtn.className = "card-remove";
    removeBtn.setAttribute("aria-label", "Remove video");
    removeBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`;
    removeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeJob(job.id, job.original_name, job.status);
    });
    card.appendChild(removeBtn);
  }

  const title = document.createElement("div");
  title.className = "card-title";
  title.textContent = job.original_name;
  title.title = job.original_name;
  card.appendChild(title);

  const badge = document.createElement("span");
  badge.className = `badge ${job.status}`;
  badge.textContent = STATUS_LABEL[job.status] || job.status;
  card.appendChild(badge);

  if (job.status === "processing" || job.status === "rendering") {
    const msg = document.createElement("div");
    msg.className = "muted card-phase";
    msg.textContent = job.status === "processing" ? "Step 1 of 2 · finding and tracking vehicles" : "Step 2 of 2 · counting each road";
    card.appendChild(msg);
    const bar = document.createElement("div");
    bar.className = "progress";
    const fill = document.createElement("span");
    fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
    bar.appendChild(fill);
    card.appendChild(bar);
  }

  if (job.status === "downloading") {
    const msg = document.createElement("div");
    msg.className = "spinner-row";
    msg.innerHTML = `<span class="signal"><i></i><i></i><i></i></span> Fetching video…`;
    card.appendChild(msg);
  }

  if (job.status === "cancelling") {
    const msg = document.createElement("div");
    msg.className = "spinner-row";
    msg.innerHTML = `<span class="signal"><i></i><i></i><i></i></span> Stopping and removing…`;
    card.appendChild(msg);
  }

  if (job.status === "error") {
    const msg = document.createElement("div");
    msg.className = "muted";
    msg.textContent = job.error || "Processing failed.";
    card.appendChild(msg);

    const actions = document.createElement("div");
    actions.className = "card-actions";
    const retryBtn = document.createElement("button");
    retryBtn.className = "retry-btn";
    retryBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 2.6 6.3M3 12V6M3 12h6"/></svg> Retry`;
    retryBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      retryJob(job.id);
    });
    actions.appendChild(retryBtn);
    card.appendChild(actions);
  }

  if (job.status === "awaiting_roads") {
    const msg = document.createElement("div");
    msg.className = "muted";
    msg.textContent = `Vehicles found. Check the ${job.road_count || ""} road outlines to get per-road counts.`;
    card.appendChild(msg);
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const btn = document.createElement("button");
    btn.className = "retry-btn primary";
    btn.textContent = "Mark roads →";
    actions.appendChild(btn);
    card.appendChild(actions);
    if (!selectMode) {
      card.addEventListener("click", () => {
        location.hash = `#/video/${job.id}`;
      });
    }
  }

  if (job.status === "done" && job.summary) {
    const stats = document.createElement("div");
    stats.className = "card-stats";
    stats.appendChild(pill(job.summary.total, "Total", ""));
    stats.appendChild(pill(job.summary.two_wheelers, "2-wheel", "two"));
    stats.appendChild(pill(job.summary.four_wheelers, "4-wheel", "four"));
    card.appendChild(stats);

    if (job.summary.roads?.length) {
      const roadsRow = document.createElement("div");
      roadsRow.className = "card-roads";
      roadsRow.innerHTML = job.summary.roads
        .map(
          (r) => `<span class="road-chip" style="--road:${r.color}" title="${escapeHtml(r.name)}">
            <i></i>${escapeHtml(shortRoadName(r.name))} <b>${r.total}</b></span>`
        )
        .join("");
      card.appendChild(roadsRow);
    }

    if (!selectMode) {
      card.addEventListener("click", () => {
        location.hash = `#/video/${job.id}`;
      });
    }
  }

  return card;
}

function pill(num, lbl, cls) {
  const p = document.createElement("div");
  p.className = "pill " + cls;
  p.innerHTML = `<span class="num">${num ?? 0}</span><span class="lbl">${lbl}</span>`;
  return p;
}

/* ---------------- Detail view ---------------- */

function stopDetailPolling() {
  if (detailTimer) {
    clearTimeout(detailTimer);
    detailTimer = null;
  }
}

/** Re-check a running job every few seconds while its page is open. */
function pollDetail(jobId) {
  stopDetailPolling();
  detailTimer = setTimeout(() => {
    if (location.hash.startsWith(`#/video/${jobId}`)) route();
  }, 2500);
}

async function showDetail(jobId, wantEditor) {
  stopDetailPolling();
  viewGallery.hidden = true;
  viewDetail.hidden = false;
  if (!detail.dataset.job || detail.dataset.job !== jobId) {
    detail.innerHTML = `<p class="spinner-row"><span class="signal"><i></i><i></i><i></i></span> Loading…</p>`;
  }
  detail.dataset.job = jobId;

  let data;
  try {
    const res = await fetch(`/api/videos/${jobId}`);
    if (!res.ok) throw new Error("not found");
    data = await res.json();
  } catch (e) {
    detail.innerHTML = `<div class="notice err">Could not load this video.</div>`;
    return;
  }

  if (data.status === "awaiting_roads" || (wantEditor && data.can_edit_roads)) {
    return showRoadEditor(jobId, data);
  }
  if (data.status !== "done") return showPending(jobId, data);
  if (!data.stats?.roads) return showLegacyDetail(jobId, data);
  return showRoadResults(jobId, data);
}

function showPending(jobId, data) {
  const isError = data.status === "error";
  const isRunning = RUNNING.includes(data.status) || data.status === "queued";
  const isCancelling = data.status === "cancelling";
  const pct = Math.round((data.progress || 0) * 100);
  const phase =
    data.status === "rendering" || (data.status === "queued" && data.stage === "render")
      ? "Step 2 of 2 · counting vehicles on each road and building the road videos"
      : data.status === "downloading"
      ? "Downloading the video…"
      : "Step 1 of 2 · finding and tracking every vehicle";
  detail.innerHTML = `<h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
    <div class="notice${isError ? " err" : ""}">${
      isError
        ? "Processing failed: " + escapeHtml(data.error || "unknown error")
        : isCancelling
        ? "Stopping and removing this video…"
        : `${escapeHtml(phase)}${data.status === "queued" ? " (waiting for its turn)" : ` · ${pct}%`}`
    }</div>
    ${isRunning && data.status !== "queued" ? `<div class="progress big"><span style="width:${pct}%"></span></div>` : ""}
    <div class="card-actions" style="margin-top:14px;">
      ${isError ? `<button class="retry-btn" id="detailRetryBtn">${ICON_RETRY} Retry</button>` : ""}
      ${isError && data.can_edit_roads ? `<a class="link" href="#/video/${jobId}/roads">Edit roads instead</a>` : ""}
      ${isCancelling ? "" : `<button class="link danger" id="detailRemoveBtn">${isRunning ? "Stop &amp; remove this video" : "Remove instead"}</button>`}
    </div>`;
  el("#detailRetryBtn")?.addEventListener("click", () => retryJob(jobId));
  el("#detailRemoveBtn")?.addEventListener("click", () => removeJob(jobId, data.original_name, data.status));
  if (isRunning || isCancelling) pollDetail(jobId);
}

/** Videos finished by an older version of the app (no per-road data). */
function showLegacyDetail(jobId, data) {
  const s = data.stats || {};
  detail.innerHTML = `
    <h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
    <div class="notice">This video was processed before per-road counting existed. Set up its roads to get counts, videos and timelines for each road.</div>
    <div class="card-actions" style="margin:14px 0 18px;">
      <button class="retry-btn primary" id="setupRoadsBtn">Set up roads</button>
      <button class="link danger" id="detailRemoveBtn">Remove this video</button>
    </div>
    <div class="video-wrap"><video src="${data.video_url}" controls playsinline></video></div>
    ${renderTiles(s)}`;
  el("#setupRoadsBtn").addEventListener("click", async () => {
    const n = await askRoadCount(data.original_name, data.road_count);
    if (n) retryJob(jobId, n);
  });
  el("#detailRemoveBtn").addEventListener("click", () => removeJob(jobId, data.original_name, data.status));
}

function renderTiles(s, extra = "") {
  return `<div class="tiles">
      <div class="tile"><div class="big">${s.total ?? 0}</div><div class="cap">Total vehicles</div></div>
      <div class="tile two"><div class="big">${s.categories?.["2-wheeler"] ?? 0}</div><div class="cap">2-wheelers</div></div>
      <div class="tile four"><div class="big">${s.categories?.["4-wheeler"] ?? 0}</div><div class="cap">4-wheelers</div></div>
      ${extra}
    </div>`;
}

const ICON_RETRY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 2.6 6.3M3 12V6M3 12h6"/></svg>`;

/* ---------------- Per-road results ---------------- */

const TYPE_COLOR = {
  car: "#f3b21f",
  motorcycle: "#4a84b8",
  bus: "#45ad6c",
  truck: "#e15646",
  bicycle: "#9aa0a3",
};
const TYPE_ORDER = ["car", "motorcycle", "bus", "truck", "bicycle"];

function fmtTime(t) {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function shortRoadName(name) {
  return String(name).replace(/\s*\(.*\)\s*$/, "");
}

function showRoadResults(jobId, data) {
  const s = data.stats;
  const roads = s.roads;
  const duration = s.duration || (s.frames && s.fps ? s.frames / s.fps : 0);

  detail.innerHTML = `
    <h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
    <div class="detail-toolbar">
      <a class="retry-btn" href="#/video/${jobId}/roads">Edit roads</a>
      <span class="muted">${roads.length} road${roads.length === 1 ? "" : "s"} · ${fmtTime(duration)} of footage</span>
    </div>

    ${renderTiles(s, `<div class="tile"><div class="big">${roads.length}</div><div class="cap">Roads</div></div>`)}

    <div class="road-legend">
      ${roads
        .map(
          (r) => `<button class="road-chip big" data-jump="${r.index}" style="--road:${r.color}">
            <i></i>${escapeHtml(r.name)} <b>${r.total}</b></button>`
        )
        .join("")}
      ${s.unassigned ? `<span class="muted" title="Parked vehicles, vehicles outside every outline, and tracker fragments picked up mid-junction aren't counted for any road, so nothing is counted twice.">${s.unassigned} not counted for a road ⓘ</span>` : ""}
    </div>

    <details class="panel overview-panel">
      <summary>Full junction view</summary>
      <div class="video-wrap"><video src="${data.video_url}" controls playsinline preload="none"></video></div>
    </details>

    ${roads.map((r) => renderRoadBlock(r, duration)).join("")}

    <div class="panel">
      <h3>Every vehicle detected (${s.vehicles.length})</h3>
      <div class="lane-divider"></div>
      ${renderVehicleTable(s.vehicles, roads)}
    </div>

    <div class="meta-strip">
      <div><span class="v">${s.frames ?? "?"}</span><span class="k">Frames</span></div>
      <div><span class="v">${s.fps ?? "?"}</span><span class="k">FPS</span></div>
      <div><span class="v">${escapeHtml(s.resolution ?? "?")}</span><span class="k">Resolution</span></div>
    </div>

    <div class="panel">
      <h3>Trim this video</h3>
      <div class="lane-divider"></div>
      <p class="hint" style="margin:0 0 10px;">Cut the source clip down to a shorter window and re-run detection on just that part. Your road outlines are kept.</p>
      <div class="trim-form">
        <label>Start (s)<input type="number" id="trimStart" min="0" step="0.5" value="0" /></label>
        <label>End (s, optional)<input type="number" id="trimEnd" min="0" step="0.5" placeholder="e.g. 20" /></label>
        <button class="retry-btn" id="trimBtn">Trim &amp; reprocess</button>
      </div>
    </div>

    <div class="card-actions" style="margin-top:18px;">
      <button class="retry-btn" id="detailReprocessBtn">${ICON_RETRY} Re-detect vehicles</button>
      <button class="link danger" id="detailRemoveBtn">Remove this video</button>
    </div>
  `;

  el("#detailRemoveBtn").addEventListener("click", () => removeJob(jobId, data.original_name, data.status));
  el("#detailReprocessBtn").addEventListener("click", () => {
    if (confirm("Run vehicle detection again from scratch? (To change road outlines only, use Edit roads — it's much faster.)"))
      retryJob(jobId);
  });
  el("#trimBtn").addEventListener("click", () => trimJob(jobId));
  detail.querySelectorAll("[data-jump]").forEach((b) =>
    b.addEventListener("click", () => el(`#road-${b.dataset.jump}`).scrollIntoView({ behavior: "smooth", block: "start" }))
  );
  roads.forEach((r) => wireRoadBlock(r, duration));
}

function renderRoadBlock(r, duration) {
  const types = TYPE_ORDER.filter((t) => r.by_type[t]).concat(
    Object.keys(r.by_type).filter((t) => !TYPE_ORDER.includes(t))
  );
  const pills = types.length
    ? types
        .map(
          (t) => `<div class="type-pill" style="--type:${TYPE_COLOR[t] || "#9aa0a3"}">
            ${vehicleIcon(t)}<span class="num">${r.by_type[t]}</span><span class="lbl">${escapeHtml(t)}</span></div>`
        )
        .join("")
    : `<p class="muted">No vehicles came from this road.</p>`;
  const events = r.events.length
    ? r.events
        .map(
          (e, i) => `<button class="event" data-i="${i}" data-t="${e.t}">
            <span class="event-t">${fmtTime(e.t)}</span>${vehicleIcon(e.type)}
            <span class="event-type">${escapeHtml(e.type)}</span><span class="event-id">#${e.id}</span></button>`
        )
        .join("")
    : `<p class="muted" style="padding:10px;">Nothing to list.</p>`;
  return `
    <section class="road-block" id="road-${r.index}" style="--road:${r.color}">
      <header class="road-head">
        <i class="road-swatch"></i>
        <h3>${escapeHtml(r.name)}</h3>
        <span class="road-total"><b>${r.total}</b> vehicle${r.total === 1 ? "" : "s"} came from here</span>
      </header>
      <div class="road-grid">
        <div class="road-video video-wrap"><video src="${r.video_url}" controls playsinline muted preload="metadata"></video></div>
        <div class="road-side">
          <div class="type-pills">${pills}</div>
          <div class="timeline">
            <div class="timeline-head"><span>Vehicles over time</span><span class="muted" data-now>0:00 · 0 so far</span></div>
            ${renderTimelineGraph(r, duration, types)}
          </div>
          <div class="event-list" data-events>${events}</div>
        </div>
      </div>
    </section>`;
}

/** Step graph of cumulative arrivals: one thick line for all vehicles, thin lines per type. */
function renderTimelineGraph(r, duration, types) {
  const W = 400, H = 130, padL = 26, padR = 8, padT = 8, padB = 18;
  const dur = Math.max(duration, 0.1);
  const maxY = Math.max(1, r.total);
  const x = (t) => padL + (Math.min(t, dur) / dur) * (W - padL - padR);
  const y = (n) => padT + (1 - n / maxY) * (H - padT - padB);

  const stepPath = (evs) => {
    let d = `M${x(0)},${y(0)}`;
    let n = 0;
    for (const e of evs) {
      d += ` H${x(e.t).toFixed(1)} V${y(++n).toFixed(1)}`;
    }
    return d + ` H${x(dur)}`;
  };
  const typeLines = types.length > 1
    ? types
        .map((t) => `<path d="${stepPath(r.events.filter((e) => e.type === t))}" stroke="${TYPE_COLOR[t] || "#9aa0a3"}" class="tl-type"/>`)
        .join("")
    : "";
  const ticks = [0, 0.5, 1].map((f) => `<text x="${x(dur * f)}" y="${H - 4}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${fmtTime(dur * f)}</text>`).join("");
  const legend = types.length > 1
    ? `<div class="tl-legend"><span><i style="background:var(--road)"></i>All</span>${types
        .map((t) => `<span><i style="background:${TYPE_COLOR[t] || "#9aa0a3"}"></i>${escapeHtml(t)}</span>`)
        .join("")}</div>`
    : "";
  return `
    <svg class="tl-graph" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-graph data-dur="${dur}" data-padl="${padL}" data-padr="${padR}" data-w="${W}">
      <line x1="${padL}" y1="${y(0)}" x2="${W - padR}" y2="${y(0)}" class="tl-axis"/>
      <line x1="${padL}" y1="${y(maxY)}" x2="${W - padR}" y2="${y(maxY)}" class="tl-grid"/>
      <text x="${padL - 4}" y="${y(maxY) + 4}" text-anchor="end">${maxY}</text>
      <text x="${padL - 4}" y="${y(0) + 4}" text-anchor="end">0</text>
      ${ticks}
      ${typeLines}
      <path d="${stepPath(r.events)}" class="tl-all"/>
      <line class="tl-playhead" data-playhead x1="${padL}" x2="${padL}" y1="${padT}" y2="${y(0)}"/>
    </svg>
    ${legend}`;
}

/** Keep the graph playhead, "so far" counter and event list in sync with the road's video. */
function wireRoadBlock(r, duration) {
  const block = el(`#road-${r.index}`);
  const video = block.querySelector("video");
  const graph = block.querySelector("[data-graph]");
  const playhead = block.querySelector("[data-playhead]");
  const nowLbl = block.querySelector("[data-now]");
  const list = block.querySelector("[data-events]");
  const buttons = [...list.querySelectorAll(".event")];
  const dur = parseFloat(graph.dataset.dur);
  const padL = parseFloat(graph.dataset.padl);
  const padR = parseFloat(graph.dataset.padr);
  const W = parseFloat(graph.dataset.w);
  let lastPassed = -2;
  let raf = null;

  const sync = () => {
    const t = video.currentTime || 0;
    const px = padL + (Math.min(t, dur) / dur) * (W - padL - padR);
    playhead.setAttribute("x1", px);
    playhead.setAttribute("x2", px);
    let passed = -1;
    while (passed + 1 < r.events.length && r.events[passed + 1].t <= t) passed++;
    nowLbl.textContent = `${fmtTime(t)} · ${passed + 1} so far`;
    if (passed !== lastPassed) {
      buttons.forEach((b, i) => {
        b.classList.toggle("past", i <= passed);
        b.classList.toggle("current", i === passed);
      });
      const cur = buttons[passed];
      if (cur && !video.paused) {
        // Scroll the list (not the page) so the latest arrival stays visible.
        const top = cur.offsetTop - list.offsetTop;
        if (top < list.scrollTop || top > list.scrollTop + list.clientHeight - cur.offsetHeight) {
          list.scrollTop = top - list.clientHeight / 2;
        }
      }
      lastPassed = passed;
    }
  };
  const loop = () => {
    sync();
    raf = video.paused ? null : requestAnimationFrame(loop);
  };
  video.addEventListener("play", () => {
    if (!raf) raf = requestAnimationFrame(loop);
  });
  video.addEventListener("seeked", sync);
  video.addEventListener("timeupdate", () => {
    if (video.paused) sync();
  });

  const seekTo = (t) => {
    video.currentTime = Math.max(0, t);
    sync();
    video.play().catch(() => {});
  };
  buttons.forEach((b) => b.addEventListener("click", () => seekTo(parseFloat(b.dataset.t) - 1)));
  graph.addEventListener("click", (e) => {
    const rect = graph.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * W;
    seekTo(((fx - padL) / (W - padL - padR)) * dur);
  });
  sync();
}

/* ---------------- Road editor ---------------- */

async function showRoadEditor(jobId, data) {
  let setup;
  try {
    const res = await fetch(`/api/videos/${jobId}/roads-setup`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    setup = await res.json();
  } catch (e) {
    detail.innerHTML = `<div class="notice err">Could not load the road editor: ${escapeHtml(String(e.message || e))}</div>`;
    return;
  }

  const W = setup.width;
  const H = setup.height;
  const toPx = (roads) => roads.map((r) => ({ name: r.name, pts: r.polygon.map(([x, y]) => [x * W, y * H]) }));
  const st = {
    roads: toPx(setup.roads),
    selected: 0,
    drawing: null, // {target: index|null (new road), pts: []}
    drag: null,
    showPaths: true,
    dirty: false,
  };
  const isRedo = data.status === "done";
  const asked = setup.road_count;

  detail.innerHTML = `
    <h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
    <div class="notice editor-intro">
      <strong>Check the road outlines.</strong> Each vehicle is counted on the road it <em>comes from</em>, so every road's
      outline should cover the stretch of road leading into the junction. Faint lines show where vehicles actually drove.
    </div>
    <div class="editor">
      <div class="editor-stage" id="edStage">
        <img src="${setup.frame_url}" alt="Still frame from the video" draggable="false" />
        <svg id="edSvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"></svg>
      </div>
      <aside class="editor-side">
        <p class="section-label" style="margin-top:0;">Roads <span class="muted" id="edCountNote"></span></p>
        <div id="edList" class="ed-list"></div>
        <div class="ed-tools">
          <button class="link" id="edAdd">+ Add road</button>
          <button class="link" id="edReset">Reset to suggestion</button>
        </div>
        <label class="ed-check"><input type="checkbox" id="edPaths" checked /> Show vehicle paths</label>
        <div class="ed-help" id="edHelp"></div>
        <button class="retry-btn primary ed-confirm" id="edConfirm">${isRedo ? "Save &amp; recount" : "Confirm roads &amp; count"}</button>
        ${isRedo ? `<a class="link" href="#/video/${jobId}">Cancel</a>` : ""}
      </aside>
    </div>`;

  const svg = el("#edSvg");
  const list = el("#edList");
  const colorOf = (i) => setup.colors[i % setup.colors.length];
  // Handle size in SVG units so it looks the same at any display width.
  const unit = () => W / Math.max(200, svg.getBoundingClientRect().width || 800);

  const toSvg = (e) => {
    const pt = svg.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    return [Math.min(Math.max(p.x, 0), W), Math.min(Math.max(p.y, 0), H)];
  };
  const centroid = (pts) => [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];

  function drawSvg() {
    const u = unit();
    let out = "";
    if (st.showPaths) {
      out += setup.trajectories
        .map((tr) => `<polyline class="ed-path" points="${tr.map(([x, y]) => `${(x * W).toFixed(1)},${(y * H).toFixed(1)}`).join(" ")}" stroke-width="${1.4 * u}"/>`)
        .join("");
    }
    st.roads.forEach((r, i) => {
      if (st.drawing && st.drawing.target === i) return;
      const sel = i === st.selected && !st.drawing;
      const pts = r.pts.map((p) => p.join(",")).join(" ");
      out += `<polygon class="ed-poly${sel ? " sel" : ""}" data-poly="${i}" points="${pts}" fill="${colorOf(i)}" stroke="${colorOf(i)}" stroke-width="${(sel ? 3 : 2) * u}"/>`;
    });
    // Labels on top of all outlines.
    st.roads.forEach((r, i) => {
      if (st.drawing && st.drawing.target === i) return;
      const [cx, cy] = centroid(r.pts);
      const fs = 13 * u;
      out += `<g class="ed-label" data-poly="${i}"><rect x="${cx - 4.2 * fs}" y="${cy - fs}" width="${8.4 * fs}" height="${1.6 * fs}" rx="${0.3 * fs}" fill="${colorOf(i)}"/>
        <text x="${cx}" y="${cy + 0.25 * fs}" font-size="${fs}" text-anchor="middle">${escapeHtml(shortRoadName(r.name))}</text></g>`;
    });
    // Handles for the selected road.
    const r = st.roads[st.selected];
    if (r && !st.drawing) {
      r.pts.forEach((p, k) => {
        const q = r.pts[(k + 1) % r.pts.length];
        out += `<circle class="ed-mid" data-mid="${k}" cx="${(p[0] + q[0]) / 2}" cy="${(p[1] + q[1]) / 2}" r="${4.5 * u}"/>`;
      });
      r.pts.forEach((p, k) => {
        out += `<circle class="ed-vtx" data-vtx="${k}" cx="${p[0]}" cy="${p[1]}" r="${7 * u}" stroke="${colorOf(st.selected)}" stroke-width="${2.5 * u}"/>`;
      });
    }
    if (st.drawing) {
      const pts = st.drawing.pts;
      const c = colorOf(st.drawing.target ?? st.roads.length);
      if (pts.length) {
        out += `<polyline class="ed-drawing" points="${pts.map((p) => p.join(",")).join(" ")}" stroke="${c}" stroke-width="${2.5 * u}"/>`;
        pts.forEach((p, k) => {
          out += `<circle class="ed-vtx${k === 0 && pts.length >= 3 ? " closer" : ""}" cx="${p[0]}" cy="${p[1]}" r="${(k === 0 ? 9 : 6) * u}" stroke="${c}" stroke-width="${2.5 * u}"/>`;
        });
      }
    }
    svg.innerHTML = out;
    svg.classList.toggle("drawing", !!st.drawing);
  }

  function drawList() {
    const n = st.roads.length;
    el("#edCountNote").textContent = asked && asked !== n ? `· you said ${asked}, now ${n}` : `· ${n}`;
    list.innerHTML = st.roads
      .map(
        (r, i) => `<div class="ed-row${i === st.selected ? " sel" : ""}" data-row="${i}" style="--road:${colorOf(i)}">
          <i class="road-swatch"></i>
          <input value="${escapeHtml(r.name)}" data-name="${i}" aria-label="Road name" maxlength="40" />
          <button class="link" data-redraw="${i}" title="Draw this outline again">Redraw</button>
          <button class="ed-del" data-del="${i}" title="Remove road" aria-label="Remove road" ${n <= 1 ? "disabled" : ""}>✕</button>
        </div>`
      )
      .join("");
    el("#edAdd").disabled = n >= setup.max_roads || !!st.drawing;
    el("#edHelp").innerHTML = st.drawing
      ? `<b>Drawing:</b> click around the road to place corners. Click the first corner (or press Enter) to finish; Esc cancels.
         <div style="margin-top:8px;"><button class="link" id="edFinish" ${st.drawing.pts.length < 3 ? "disabled" : ""}>Finish</button>
         <button class="link" id="edCancelDraw">Cancel</button></div>`
      : `Drag a corner to move it · drag the small dots to add a corner · double-click a corner to delete it · drag inside an outline to move it all.`;
    el("#edFinish")?.addEventListener("click", finishDrawing);
    el("#edCancelDraw")?.addEventListener("click", cancelDrawing);
  }

  function redraw() {
    drawSvg();
    drawList();
  }

  function startDrawing(target) {
    st.drawing = { target, pts: [] };
    redraw();
  }
  function cancelDrawing() {
    st.drawing = null;
    redraw();
  }
  function finishDrawing() {
    const d = st.drawing;
    if (!d || d.pts.length < 3) return;
    if (d.target === null) {
      st.roads.push({ name: `Road ${st.roads.length + 1}`, pts: d.pts });
      st.selected = st.roads.length - 1;
    } else {
      st.roads[d.target].pts = d.pts;
      st.selected = d.target;
    }
    st.drawing = null;
    st.dirty = true;
    redraw();
  }

  // --- pointer interaction on the frame
  svg.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const p = toSvg(e);
    if (st.drawing) {
      const pts = st.drawing.pts;
      const u = unit();
      if (pts.length >= 3 && Math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) < 12 * u) {
        finishDrawing();
      } else {
        pts.push(p);
        redraw();
      }
      return;
    }
    const t = e.target;
    const r = st.roads[st.selected];
    if (t.dataset.vtx !== undefined) {
      st.drag = { kind: "vtx", k: +t.dataset.vtx };
    } else if (t.dataset.mid !== undefined) {
      const k = +t.dataset.mid;
      r.pts.splice(k + 1, 0, p);
      st.drag = { kind: "vtx", k: k + 1 };
      st.dirty = true;
    } else {
      const polyEl = t.closest("[data-poly]");
      if (polyEl) {
        st.selected = +polyEl.dataset.poly;
        st.drag = { kind: "poly", last: p };
        redraw();
      } else {
        return;
      }
    }
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    if (!st.drag) return;
    const p = toSvg(e);
    const r = st.roads[st.selected];
    if (st.drag.kind === "vtx") {
      r.pts[st.drag.k] = p;
    } else {
      const dx = p[0] - st.drag.last[0];
      const dy = p[1] - st.drag.last[1];
      r.pts = r.pts.map(([x, y]) => [Math.min(Math.max(x + dx, 0), W), Math.min(Math.max(y + dy, 0), H)]);
      st.drag.last = p;
    }
    st.dirty = true;
    drawSvg();
  });
  const endDrag = () => {
    st.drag = null;
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("dblclick", (e) => {
    const k = e.target.dataset?.vtx;
    const r = st.roads[st.selected];
    if (k !== undefined && r && r.pts.length > 3) {
      r.pts.splice(+k, 1);
      st.dirty = true;
      drawSvg();
    }
  });
  const onKey = (e) => {
    if (!document.body.contains(svg)) return document.removeEventListener("keydown", onKey);
    if (!st.drawing) return;
    if (e.key === "Enter") finishDrawing();
    if (e.key === "Escape") cancelDrawing();
  };
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => document.body.contains(svg) && drawSvg());

  // --- side panel
  list.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.del !== undefined) {
      const i = +t.dataset.del;
      if (st.roads.length <= 1) return;
      st.roads.splice(i, 1);
      st.selected = Math.min(st.selected, st.roads.length - 1);
      st.dirty = true;
      return redraw();
    }
    if (t.dataset.redraw !== undefined) return startDrawing(+t.dataset.redraw);
    const row = t.closest("[data-row]");
    if (row && t.tagName !== "INPUT") {
      st.selected = +row.dataset.row;
      redraw();
    }
  });
  list.addEventListener("focusin", (e) => {
    const i = e.target.dataset?.name;
    if (i !== undefined && +i !== st.selected) {
      st.selected = +i;
      drawSvg();
      list.querySelectorAll(".ed-row").forEach((row) => row.classList.toggle("sel", +row.dataset.row === st.selected));
    }
  });
  list.addEventListener("input", (e) => {
    const i = e.target.dataset?.name;
    if (i !== undefined) {
      st.roads[+i].name = e.target.value;
      st.dirty = true;
      drawSvg();
    }
  });
  el("#edAdd").addEventListener("click", () => startDrawing(null));
  el("#edReset").addEventListener("click", () => {
    if (st.dirty && !confirm("Throw away your changes and go back to the suggested outlines?")) return;
    st.roads = toPx(setup.suggested_roads);
    st.selected = 0;
    st.drawing = null;
    st.dirty = false;
    redraw();
  });
  el("#edPaths").addEventListener("change", (e) => {
    st.showPaths = e.target.checked;
    drawSvg();
  });
  el("#edConfirm").addEventListener("click", async () => {
    if (st.drawing) return showToast("Finish or cancel the outline you're drawing first.");
    const btn = el("#edConfirm");
    btn.disabled = true;
    const body = {
      roads: st.roads.map((r, i) => ({
        name: r.name.trim() || `Road ${i + 1}`,
        polygon: r.pts.map(([x, y]) => [x / W, y / H]),
      })),
    };
    try {
      const res = await fetch(`/api/videos/${jobId}/roads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(`Could not save roads: ${err.detail || res.statusText}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      showToast(`Could not save roads: ${e}`);
      btn.disabled = false;
      return;
    }
    showToast("Roads saved — counting each road…");
    if (location.hash === `#/video/${jobId}`) route();
    else location.hash = `#/video/${jobId}`;
  });

  // The image decides the stage size; draw once it's laid out.
  const img = detail.querySelector(".editor-stage img");
  if (img.complete) redraw();
  else img.addEventListener("load", redraw);
  redraw();
}

/* ---------------- Shared bits ---------------- */

/* Small inline vehicle icons, matched to the ones in the header's traffic strip. */
const VEHICLE_ICON = {
  car: `<svg viewBox="0 0 48 28"><rect x="3" y="10" width="38" height="9" rx="3" fill="#f3b21f"/><path d="M11 10 L15 3 H31 L35 10 Z" fill="#f3b21f"/><rect x="16" y="5" width="6" height="5" fill="#232628"/><rect x="24" y="5" width="6" height="5" fill="#232628"/><circle cx="13" cy="20" r="4.2" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/><circle cx="33" cy="20" r="4.2" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/></svg>`,
  bus: `<svg viewBox="0 0 54 28"><rect x="3" y="4" width="46" height="16" rx="3" fill="#45ad6c"/><rect x="8" y="8" width="7" height="6" rx="1" fill="#232628"/><rect x="18" y="8" width="7" height="6" rx="1" fill="#232628"/><rect x="28" y="8" width="7" height="6" rx="1" fill="#232628"/><rect x="38" y="8" width="7" height="6" rx="1" fill="#232628"/><circle cx="14" cy="22" r="4" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/><circle cx="40" cy="22" r="4" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/></svg>`,
  truck: `<svg viewBox="0 0 48 28"><rect x="2" y="9" width="17" height="10" rx="2" fill="#e15646"/><path d="M19 9 H33 a3 3 0 0 1 3 3 v7 H19 Z" fill="#e15646" opacity="0.75"/><rect x="6" y="12" width="8" height="6" fill="#232628"/><circle cx="11" cy="20" r="4" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/><circle cx="30" cy="20" r="4" fill="#232628" stroke="#f4f1e8" stroke-width="1.4"/></svg>`,
  motorcycle: `<svg viewBox="0 0 48 28"><path d="M8 20 L20 20 L27 10 L34 10" stroke="#4a84b8" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M27 10 L31 20" stroke="#4a84b8" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M31 8 L38 8" stroke="#4a84b8" stroke-width="2.5" stroke-linecap="round"/><circle cx="10" cy="20" r="5.2" fill="#232628" stroke="#4a84b8" stroke-width="1.6"/><circle cx="34" cy="20" r="5.2" fill="#232628" stroke="#4a84b8" stroke-width="1.6"/></svg>`,
  bicycle: `<svg viewBox="0 0 40 28"><path d="M8 20 L17 20 L22 9 L26 9" stroke="#9aa0a3" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M17 20 L23 20 L18 12 L13 12 Z" stroke="#9aa0a3" stroke-width="2" fill="none" stroke-linejoin="round"/><path d="M13 12 L17 20" stroke="#9aa0a3" stroke-width="2" fill="none"/><path d="M26 9 L29 9 M22 9 L18 12" stroke="#9aa0a3" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="8" cy="20" r="5.5" fill="none" stroke="#9aa0a3" stroke-width="1.8"/><circle cx="23" cy="20" r="5.5" fill="none" stroke="#9aa0a3" stroke-width="1.8"/></svg>`,
};

function vehicleIcon(type) {
  const svg = VEHICLE_ICON[type];
  return svg ? `<span class="v-icon">${svg}</span>` : "";
}

function vehicleIcon(type) {
  const svg = VEHICLE_ICON[type];
  return svg ? `<span class="v-icon">${svg}</span>` : "";
}

function renderVehicleTable(vehicles, roads) {
  if (!vehicles.length) return `<p class="muted">No vehicles detected.</p>`;
  const rows = [...vehicles]
    .sort((a, b) => a.t - b.t)
    .map((v) => {
      const catClass = v.category === "2-wheeler" ? "two" : "four";
      const road = v.road !== null && v.road !== undefined ? roads[v.road] : null;
      return `<tr>
        <td>#${v.id}</td>
        <td>${vehicleIcon(v.type)}${escapeHtml(v.type)}</td>
        <td><span class="tag ${catClass}">${escapeHtml(v.category)}</span></td>
        <td>${road ? `<span class="road-chip" style="--road:${road.color}"><i></i>${escapeHtml(shortRoadName(road.name))}</span>` : `<span class="muted">—</span>`}</td>
        <td>${fmtTime(v.t)}</td>
        <td>${(v.confidence * 100).toFixed(0)}%</td>
      </tr>`;
    })
    .join("");
  return `<div class="table-scroll"><table>
    <thead><tr><th>ID</th><th>Type</th><th>Category</th><th>Came from</th><th>Arrived</th><th>Avg conf.</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ---------------- Router ---------------- */

function route() {
  const hash = location.hash;
  const m = hash.match(/^#\/video\/([a-f0-9]+)(\/roads)?$/i);
  if (m) {
    showDetail(m[1], !!m[2]);
  } else {
    stopDetailPolling();
    detail.dataset.job = "";
    viewDetail.hidden = true;
    viewGallery.hidden = false;
    refreshGallery();
  }
}

el("#backBtn").addEventListener("click", () => {
  location.hash = "#/";
});

window.addEventListener("hashchange", route);
route();
