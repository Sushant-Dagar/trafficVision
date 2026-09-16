"""A tiny thread-safe, file-backed job store.

Persists all jobs to ``storage/jobs.json`` as a single JSON list. This is
deliberately simple (no database) since the app processes a handful of videos on
one machine. A module-level lock serialises reads/writes so the background worker
thread and the request handlers don't clobber the file.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Optional

from . import config

# Valid statuses: downloading -> queued -> processing -> done | error
# ("cancelling" is a transitional status for a downloading/processing job
# whose removal was requested — see request_cancel below.)
_LOCK = threading.RLock()

# In-memory set of job ids whose removal was requested while they were still
# downloading or processing. Checked cheaply (no disk I/O) by the download
# and frame-processing loops so they can stop themselves and clean up, rather
# than the request handler racing the worker thread by deleting files/records
# out from under it. Not persisted — a server restart naturally "cancels"
# anything mid-flight anyway (the worker thread doesn't survive it).
_CANCELLED: set[str] = set()


def request_cancel(job_id: str) -> None:
    """Ask a running (downloading/processing) job to stop and be removed.

    The job isn't deleted here — the worker thread notices the flag, stops
    itself, cleans up its own partial files, and deletes the job record. This
    avoids the request handler deleting files while the worker is still
    writing to them.
    """
    _CANCELLED.add(job_id)
    update_job(job_id, status="cancelling")


def is_cancelled(job_id: str) -> bool:
    return job_id in _CANCELLED


def clear_cancel(job_id: str) -> None:
    _CANCELLED.discard(job_id)


def _read_all() -> list[dict[str, Any]]:
    if not config.JOBS_FILE.exists():
        return []
    try:
        with config.JOBS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        # Corrupt/partial file: start clean rather than crash the whole app.
        return []


def _write_all(jobs: list[dict[str, Any]]) -> None:
    # Write to a temp file then replace, so a crash mid-write can't corrupt the
    # store (os.replace is atomic on Windows and POSIX).
    tmp = config.JOBS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=2)
    tmp.replace(config.JOBS_FILE)


def create_job(
    original_name: str,
    stored_name: str,
    source_url: str | None = None,
    status: str = "queued",
    pending_trim: dict | None = None,
) -> dict[str, Any]:
    """Register a video job and return its record.

    ``source_url`` is set for videos fetched from a remote URL (YouTube / Dropbox /
    direct). Such jobs typically start with ``status="downloading"`` and only get a
    real ``stored_name`` once the download finishes.

    ``pending_trim``, if given, is ``{"start": float, "end": float|None}`` — a trim
    window requested at add-time that hasn't been applied yet because the source
    file doesn't exist locally yet (a remote URL still downloading). The worker
    applies it once the download completes and clears the field.
    """
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "original_name": original_name,
        "stored_name": stored_name,       # filename inside storage/uploads
        "source_url": source_url,          # None for local uploads
        "status": status,                  # downloading | queued | processing | done | error
        "created_at": time.time(),
        "updated_at": time.time(),
        "progress": 0.0,                   # 0..1 during processing
        "error": None,
        "summary": None,                   # populated when done (see processor)
        "pending_trim": pending_trim,      # {"start", "end"} or None
    }
    with _LOCK:
        jobs = _read_all()
        jobs.append(job)
        _write_all(jobs)
    return job


def create_url_job(url: str, label: str | None = None, trim: dict | None = None) -> dict[str, Any]:
    """Register a remote-URL job in the ``downloading`` state (file not yet fetched)."""
    return create_job(
        original_name=label or url,
        stored_name="pending",
        source_url=url,
        status="downloading",
        pending_trim=trim,
    )


def update_job(job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Patch fields on a job and stamp ``updated_at``. Returns the updated record."""
    with _LOCK:
        jobs = _read_all()
        for job in jobs:
            if job["id"] == job_id:
                job.update(fields)
                job["updated_at"] = time.time()
                _write_all(jobs)
                return job
    return None


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        for job in _read_all():
            if job["id"] == job_id:
                return job
    return None


def delete_job(job_id: str) -> bool:
    """Remove a job record. Returns True if a job was actually removed."""
    clear_cancel(job_id)
    with _LOCK:
        all_jobs = _read_all()
        remaining = [j for j in all_jobs if j["id"] != job_id]
        if len(remaining) == len(all_jobs):
            return False
        _write_all(remaining)
        return True


def list_jobs() -> list[dict[str, Any]]:
    """All jobs, newest first."""
    with _LOCK:
        jobs = _read_all()
    return sorted(jobs, key=lambda j: j.get("created_at", 0), reverse=True)


def existing_source_urls() -> set[str]:
    """All source URLs already present in the store (for seed de-duplication)."""
    with _LOCK:
        return {j["source_url"] for j in _read_all() if j.get("source_url")}
