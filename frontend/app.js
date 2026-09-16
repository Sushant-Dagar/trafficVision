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

  const body = { url };
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

  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
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

/* ---------------- Remove / retry ---------------- */

async function removeJob(jobId, name, status) {
  const isActive = status === "downloading" || status === "processing";
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

async function retryJob(jobId) {
  try {
    const res = await fetch(`/api/videos/${jobId}/retry`, { method: "POST" });
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
  showDetail(jobId);
}

/* ---------------- Gallery ---------------- */

const STATUS_LABEL = {
  downloading: "Downloading",
  queued: "Queued",
  processing: "Processing",
  cancelling: "Cancelling…",
  done: "Done",
  error: "Error",
};

const ACTIVE_STATUSES = ["downloading", "queued", "processing", "cancelling"];

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
  card.className = "card" + (job.status === "done" ? "" : " disabled") + (selectMode ? " selectable" : "");

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

  if (job.status === "processing") {
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

  if (job.status === "done" && job.summary) {
    const stats = document.createElement("div");
    stats.className = "card-stats";
    stats.appendChild(pill(job.summary.total, "Total", ""));
    stats.appendChild(pill(job.summary.two_wheelers, "2-wheel", "two"));
    stats.appendChild(pill(job.summary.four_wheelers, "4-wheel", "four"));
    card.appendChild(stats);

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

async function showDetail(jobId) {
  viewGallery.hidden = true;
  viewDetail.hidden = false;
  detail.innerHTML = `<p class="spinner-row"><span class="signal"><i></i><i></i><i></i></span> Loading…</p>`;

  let data;
  try {
    const res = await fetch(`/api/videos/${jobId}`);
    if (!res.ok) throw new Error("not found");
    data = await res.json();
  } catch (e) {
    detail.innerHTML = `<div class="notice err">Could not load this video.</div>`;
    return;
  }

  if (data.status !== "done") {
    const isError = data.status === "error";
    const isActive = data.status === "downloading" || data.status === "processing";
    const isCancelling = data.status === "cancelling";
    detail.innerHTML = `<h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
      <div class="notice${isError ? " err" : ""}">${isError
        ? "Processing failed: " + escapeHtml(data.error || "unknown error")
        : isCancelling
        ? "Stopping and removing this video…"
        : "This video is still being processed. Come back in a moment."}</div>
      ${isError ? `<div class="card-actions" style="margin-top:14px;">
          <button class="retry-btn" id="detailRetryBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 2.6 6.3M3 12V6M3 12h6"/></svg> Retry</button>
          <button class="link" id="detailRemoveBtn">Remove instead</button>
        </div>` : ""}
      ${isActive ? `<div class="card-actions" style="margin-top:14px;">
          <button class="link danger" id="detailRemoveBtn">Stop &amp; remove this video</button>
        </div>` : ""}`;
    if (isError || isActive) {
      if (isError) el("#detailRetryBtn").addEventListener("click", () => retryJob(jobId));
      el("#detailRemoveBtn").addEventListener("click", () => removeJob(jobId, data.original_name, data.status));
    }
    return;
  }

  const s = data.stats || {};
  const byType = s.by_type || {};
  const vehicles = s.vehicles || [];
  const road = s.road_layout || {};

  detail.innerHTML = `
    <h2 class="detail-title">${escapeHtml(data.original_name)}</h2>
    <div class="video-wrap">
      <span class="vf tl"></span><span class="vf tr"></span><span class="vf bl"></span><span class="vf br"></span>
      <video src="${data.video_url}" controls playsinline></video>
    </div>

    <div class="tiles">
      <div class="tile"><div class="big">${s.total ?? 0}</div><div class="cap">Total vehicles</div></div>
      <div class="tile two"><div class="big">${s.categories?.["2-wheeler"] ?? 0}</div><div class="cap">2-wheelers</div></div>
      <div class="tile four"><div class="big">${s.categories?.["4-wheeler"] ?? 0}</div><div class="cap">4-wheelers</div></div>
    </div>

    <div class="panel">
      <h3>Breakdown by type</h3>
      <div class="lane-divider"></div>
      ${renderTypeTable(byType)}
    </div>

    <div class="panel">
      <h3>Every vehicle detected (${vehicles.length})</h3>
      <div class="lane-divider"></div>
      ${renderVehicleTable(vehicles)}
    </div>

    <div class="panel">
      <h3>Estimated road layout</h3>
      <div class="lane-divider"></div>
      ${renderRoadLayout(road, jobId, data.roadmap_url)}
    </div>

    <div class="meta-strip">
      <div><span class="v">${s.frames ?? "?"}</span><span class="k">Frames</span></div>
      <div><span class="v">${s.fps ?? "?"}</span><span class="k">FPS</span></div>
      <div><span class="v">${escapeHtml(s.resolution ?? "?")}</span><span class="k">Resolution</span></div>
    </div>

    <div class="panel">
      <h3>Trim this video</h3>
      <div class="lane-divider"></div>
      <p class="hint" style="margin:0 0 10px;">Cut the source clip down to a shorter window, then re-run detection on just that part. This replaces the annotated video and stats above.</p>
      <div class="trim-form">
        <label>Start (s)<input type="number" id="trimStart" min="0" step="0.5" value="0" /></label>
        <label>End (s, optional)<input type="number" id="trimEnd" min="0" step="0.5" placeholder="e.g. 20" /></label>
        <button class="retry-btn" id="trimBtn">Trim &amp; reprocess</button>
      </div>
    </div>

    <div class="card-actions" style="margin-top:18px;">
      <button class="retry-btn" id="detailReprocessBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 2.6 6.3M3 12V6M3 12h6"/></svg> Reprocess</button>
      <button class="link danger" id="detailRemoveBtn">Remove this video</button>
    </div>
  `;
  el("#detailRemoveBtn").addEventListener("click", () => removeJob(jobId, data.original_name, data.status));
  el("#detailReprocessBtn").addEventListener("click", () => retryJob(jobId));
  el("#trimBtn").addEventListener("click", () => trimJob(jobId));
  const roadReprocessBtn = el("#roadReprocessBtn");
  if (roadReprocessBtn) roadReprocessBtn.addEventListener("click", () => retryJob(jobId));
}

function renderRoadLayout(road, jobId, roadmapUrl) {
  if (!road || !road.arms_estimate) {
    const isGenuinelySparse = road && road.note && road.note.includes("Not enough moving vehicles");
    return `
      <p class="muted">${escapeHtml(road?.note || "This video was processed before road-layout estimation was added.")}</p>
      ${isGenuinelySparse ? "" : `<button class="retry-btn" id="roadReprocessBtn" style="margin-top:10px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 2.6 6.3M3 12V6M3 12h6"/></svg> Reprocess to calculate</button>`}
    `;
  }
  const snapshot = roadmapUrl
    ? `<div class="road-snapshot"><img src="${roadmapUrl}" alt="Video frame with estimated roads and lanes marked" loading="lazy" /></div>`
    : "";
  const rows = (road.directions || [])
    .map((d) => {
      const compass = headingToCompass(d.heading_deg);
      return `<tr>
        <td>${compass} <span class="muted">(${d.heading_deg}°)</span></td>
        <td>${d.lanes_estimate}</td>
        <td>${d.vehicle_count}</td>
      </tr>`;
    })
    .join("");
  const coverage = road.vehicles_total
    ? `Based on ${road.vehicles_used ?? 0} of ${road.vehicles_total} detected vehicles that had a clear, sustained direction of travel — the rest were stationary, too brief, or didn't share a route with enough other vehicles to count.`
    : "";
  return `
    ${snapshot}
    <div class="road-summary">
      <div class="road-figure">
        <span class="big">${road.arms_estimate}</span>
        <span class="cap">Approach${road.arms_estimate === 1 ? "" : "es"} detected (≈${road.arms_estimate}-way)</span>
      </div>
      <div class="road-figure">
        <span class="big">${road.roads_estimate ?? "?"}</span>
        <span class="cap">Distinct road${road.roads_estimate === 1 ? "" : "s"} crossing here</span>
      </div>
    </div>
    <table>
      <thead><tr><th>Approach direction</th><th>Lanes (est.)</th><th>Vehicles seen</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="muted road-note">${escapeHtml(coverage)}</p>
    <p class="muted road-note">${escapeHtml(road.note || "")}</p>
  `;
}

function headingToCompass(deg) {
  const dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"];
  const idx = Math.round(((deg % 360) / 45)) % 8;
  return dirs[idx];
}

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

function renderTypeTable(byType) {
  const entries = Object.entries(byType);
  if (!entries.length) return `<p class="muted">No vehicles detected.</p>`;
  const rows = entries
    .sort((a, b) => b[1] - a[1])
    .map(([type, count]) => `<tr><td>${vehicleIcon(type)}${escapeHtml(type)}</td><td>${count}</td></tr>`)
    .join("");
  return `<table><thead><tr><th>Type</th><th>Count</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderVehicleTable(vehicles) {
  if (!vehicles.length) return `<p class="muted">No vehicles detected.</p>`;
  const rows = vehicles
    .map((v) => {
      const catClass = v.category === "2-wheeler" ? "two" : "four";
      return `<tr>
        <td>#${v.id}</td>
        <td>${vehicleIcon(v.type)}${escapeHtml(v.type)}</td>
        <td><span class="tag ${catClass}">${escapeHtml(v.category)}</span></td>
        <td>${v.wheels}</td>
        <td>${(v.confidence * 100).toFixed(0)}%</td>
      </tr>`;
    })
    .join("");
  return `<table>
    <thead><tr><th>ID</th><th>Type</th><th>Category</th><th>Wheels</th><th>Avg conf.</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ---------------- Router ---------------- */

function route() {
  const hash = location.hash;
  const m = hash.match(/^#\/video\/([a-f0-9]+)$/i);
  if (m) {
    showDetail(m[1]);
  } else {
    viewDetail.hidden = true;
    viewGallery.hidden = false;
    refreshGallery();
  }
}

el("#backBtn").addEventListener("click", () => {
  location.hash = "#/";
});

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
route();
