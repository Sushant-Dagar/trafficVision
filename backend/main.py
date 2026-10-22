"""FastAPI application: upload, list, detail, and video streaming endpoints.

Long-running video processing is handled by a single background worker thread
that pulls job ids off a queue and processes them one at a time. Doing them
sequentially (rather than via many parallel background tasks) keeps CPU usage
sane on a machine without a GPU.
"""
from __future__ import annotations

import queue
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, downloader, jobs, processor, roads as roadgeo, trimmer

# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
_work_queue: "queue.Queue[str]" = queue.Queue()


def _cleanup_job_files(job_id: str) -> None:
    """Remove any files a job left behind in uploads/outputs."""
    for directory in (config.UPLOAD_DIR, config.OUTPUT_DIR):
        for path in directory.glob(f"{job_id}*"):
            try:
                path.unlink()
            except OSError:
                pass


def _download_if_needed(job_id: str) -> bool:
    """If the job is a remote-URL job whose file isn't fetched yet, download it.

    Returns True if the job is ready to process, False if it errored out.
    """
    job = jobs.get_job(job_id)
    if job is None:
        return False

    source_url = job.get("source_url")
    already_have_file = job.get("stored_name") and job["stored_name"] != "pending"
    if not source_url or already_have_file:
        return True  # local upload, or already downloaded

    jobs.update_job(job_id, status="downloading", progress=0.0)
    try:
        dest_base = config.UPLOAD_DIR / job_id  # extension chosen by downloader
        title, actual_path = downloader.download_video(
            source_url, dest_base, cancel_check=lambda: jobs.is_cancelled(job_id)
        )
        stored_name = Path(actual_path).name
        # Prefer the video's real title for display, unless we set a nicer label.
        original_name = job.get("original_name") or title
        if not original_name or original_name == source_url:
            original_name = title
        updated = jobs.update_job(
            job_id,
            stored_name=stored_name,
            original_name=original_name,
            status="queued",
        )

        pending_trim = (updated or {}).get("pending_trim")
        if pending_trim:
            try:
                trimmed_name = trimmer.trim_source(
                    job_id, stored_name, pending_trim.get("start", 0), pending_trim.get("end")
                )
                jobs.update_job(job_id, stored_name=trimmed_name, pending_trim=None)
            except trimmer.TrimError as exc:
                jobs.update_job(
                    job_id,
                    status="error",
                    error=f"Downloaded OK, but the requested trim failed: {exc}",
                    pending_trim=None,
                )
                return False
        return True
    except downloader.DownloadCancelled:
        _cleanup_job_files(job_id)
        jobs.delete_job(job_id)
        return False
    except Exception as exc:  # noqa: BLE001
        jobs.update_job(job_id, status="error", error=f"Download failed: {exc}")
        return False


# Outputs are rewritten in place when roads are edited or a video is reprocessed,
# so make browsers check for a newer file instead of reusing a cached one.
NO_CACHE = {"Cache-Control": "no-cache"}

# Statuses where the worker thread is actively using the job's files.
RUNNING_STATUSES = ("downloading", "processing", "rendering")


def _worker_loop() -> None:
    while True:
        job_id = _work_queue.get()
        try:
            job = jobs.get_job(job_id)
            if job is None:
                continue
            if job.get("stage") == "render":
                processor.render_job(job_id)
            elif _download_if_needed(job_id):
                processor.analyse_video(job_id)
        except Exception:  # noqa: BLE001 - never let the worker thread die
            import traceback

            traceback.print_exc()
        finally:
            _work_queue.task_done()


def _seed_gallery() -> None:
    """On first run, enqueue the configured seed videos (deduped by source URL)."""
    if config.SEED_VIDEOS_DISABLED:
        return
    have = jobs.existing_source_urls()
    for seed in config.SEED_VIDEOS:
        if seed["url"] in have:
            continue
        job = jobs.create_url_job(seed["url"], label=seed.get("label"))
        _work_queue.put(job["id"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the single worker thread.
    threading.Thread(target=_worker_loop, daemon=True, name="video-worker").start()

    # Anything that was mid-flight when the server last stopped needs your
    # explicit OK to resume — rather than silently restarting (and, for a
    # `downloading` job, re-fetching the video from scratch) every time the
    # server restarts. `queued` jobs are safe to auto-resume: they hadn't
    # started yet, so nothing was interrupted or needs re-fetching.
    for job in jobs.list_jobs():
        if job["status"] == "queued":
            _work_queue.put(job["id"])
        elif job["status"] in RUNNING_STATUSES:
            jobs.update_job(
                job["id"],
                status="error",
                progress=0.0,
                error=(
                    "Interrupted when the server was last restarted — "
                    "click Retry to resume."
                ),
            )

    # Populate the gallery with seed videos on first run.
    _seed_gallery()

    yield


app = FastAPI(title="Traffic Intersection Vehicle Detection", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
def _parse_trim_window(start: Any, end: Any, duration: Any) -> dict | None:
    """Turn raw start/end/duration inputs into {"start": float, "end": float|None}.

    Returns None if nothing was actually specified (no trim requested).
    Raises ValueError with a user-facing message on invalid input.
    """
    if start in (None, ""):
        start = 0
    try:
        start = float(start)
    except (TypeError, ValueError):
        raise ValueError("'start' must be a number.")
    if start < 0:
        raise ValueError("Start time can't be negative.")

    if duration not in (None, ""):
        try:
            end = start + float(duration)
        except (TypeError, ValueError):
            raise ValueError("'duration' must be a number.")
    elif end not in (None, ""):
        try:
            end = float(end)
        except (TypeError, ValueError):
            raise ValueError("'end' must be a number.")
        if end <= start:
            raise ValueError("End time must be after the start time.")
    else:
        end = None

    if start <= 0 and end is None:
        return None  # nothing to trim
    return {"start": start, "end": end}


def _parse_road_count(raw: Any) -> int:
    """Number of roads meeting at the junction (default 4)."""
    if raw in (None, ""):
        return 4
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError("Number of roads must be a whole number.")
    if not 1 <= n <= roadgeo.MAX_ROADS:
        raise ValueError(f"Number of roads must be between 1 and {roadgeo.MAX_ROADS}.")
    return n


@app.post("/api/videos")
async def upload_video(
    file: UploadFile = File(...),
    trim_start: Any = Form(None),
    trim_end: Any = Form(None),
    trim_duration: Any = Form(None),
    road_count: Any = Form(None),
):
    """Accept a video upload, register a job, and queue it for processing.

    Optional ``trim_start``/``trim_end`` (or ``trim_duration``) fields trim the
    file down to that window *before* it's ever processed, so detection only
    runs on the part you actually want.
    """
    try:
        trim_window = _parse_trim_window(trim_start, trim_end, trim_duration)
        n_roads = _parse_road_count(road_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    original_name = file.filename or "video"
    ext = Path(original_name).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: "
            + ", ".join(sorted(config.ALLOWED_EXTENSIONS)),
        )

    # Create the job first so we get an id to name the stored file with.
    stored_name = "pending"  # placeholder; replaced below
    job = jobs.create_job(original_name=original_name, stored_name=stored_name, road_count=n_roads)
    stored_name = f"{job['id']}{ext}"
    dest = config.UPLOAD_DIR / stored_name

    # Stream the upload to disk (handles large files without loading into RAM).
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()

    if trim_window:
        try:
            stored_name = trimmer.trim_source(
                job["id"], stored_name, trim_window["start"], trim_window["end"]
            )
        except trimmer.TrimError as exc:
            # Clean up the untrimmed upload and the job record rather than
            # leaving an orphaned file/job behind.
            (config.UPLOAD_DIR / stored_name).unlink(missing_ok=True)
            jobs.delete_job(job["id"])
            raise HTTPException(status_code=400, detail=f"Trim failed: {exc}") from exc

    jobs.update_job(job["id"], stored_name=stored_name)
    _work_queue.put(job["id"])

    return {"id": job["id"], "status": "queued", "original_name": original_name}


@app.post("/api/videos/url")
async def add_video_from_url(payload: dict):
    """Fetch a video from a YouTube / Dropbox / direct URL and queue it.

    A YouTube *playlist* URL is expanded into individual videos, each queued
    separately. Optional ``start``/``end`` (or ``duration``) fields trim every
    resulting video to that window once its download finishes, before it's
    processed for the first time.

    Returns the list of created job ids.
    """
    payload = payload or {}
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please provide a video URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must start with http(s)://")

    try:
        trim_window = _parse_trim_window(
            payload.get("start"), payload.get("end"), payload.get("duration")
        )
        n_roads = _parse_road_count(payload.get("road_count"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Expand playlists into individual video URLs.
    urls = [url]
    try:
        if downloader.is_playlist(url):
            expanded = downloader.extract_playlist_urls(url)
            if expanded:
                urls = expanded
    except Exception:  # noqa: BLE001 - fall back to treating it as a single URL
        urls = [url]

    created = []
    for u in urls:
        job = jobs.create_url_job(u, trim=trim_window, road_count=n_roads)
        _work_queue.put(job["id"])
        created.append(job["id"])

    return {"ids": created, "count": len(created)}


@app.get("/api/videos")
async def list_videos():
    """Summary list of all jobs for the gallery."""
    result = []
    for job in jobs.list_jobs():
        result.append(
            {
                "id": job["id"],
                "original_name": job["original_name"],
                "status": job["status"],
                "progress": job.get("progress", 0.0),
                "created_at": job.get("created_at"),
                "error": job.get("error"),
                "summary": job.get("summary"),
                "source_url": job.get("source_url"),
                "road_count": job.get("road_count"),
            }
        )
    return result


@app.get("/api/videos/{job_id}")
async def video_detail(job_id: str):
    """Full detail for one job, including the aggregated stats JSON if done."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    stats = None
    stats_path = config.OUTPUT_DIR / f"{job_id}.json"
    if job["status"] == "done" and stats_path.exists():
        import json

        with stats_path.open("r", encoding="utf-8") as fh:
            stats = json.load(fh)

    return {
        "id": job["id"],
        "original_name": job["original_name"],
        "status": job["status"],
        "progress": job.get("progress", 0.0),
        "error": job.get("error"),
        "summary": job.get("summary"),
        "source_url": job.get("source_url"),
        "stats": stats,
        "road_count": job.get("road_count"),
        "stage": job.get("stage"),
        "can_edit_roads": processor.tracks_path(job_id).exists() and job["status"] not in RUNNING_STATUSES + ("cancelling",),
        "video_url": (
            f"/api/videos/{job_id}/video?v={(stats or {}).get('version', 0)}"
            if job["status"] == "done"
            else None
        ),
    }


@app.get("/api/videos/{job_id}/video")
async def get_output_video(job_id: str):
    """Serve the annotated MP4. FileResponse handles HTTP range requests so the
    browser <video> element can seek."""
    path = config.OUTPUT_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output video not ready")
    return FileResponse(
        str(path), media_type="video/mp4", filename=f"{job_id}.mp4", headers=NO_CACHE
    )


@app.get("/api/videos/{job_id}/roads/{index}/video")
async def get_road_video(job_id: str, index: int):
    """Serve the video cropped to one road."""
    path = processor.road_video_path(job_id, index)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Road video not ready")
    return FileResponse(
        str(path), media_type="video/mp4", filename=f"{job_id}_road{index + 1}.mp4", headers=NO_CACHE
    )


@app.get("/api/videos/{job_id}/frame")
async def get_reference_frame(job_id: str):
    """A clean still from the video, used as the road editor's background."""
    path = processor.frame_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Frame not available yet")
    return FileResponse(str(path), media_type="image/jpeg", headers=NO_CACHE)


@app.get("/api/videos/{job_id}/roads-setup")
async def roads_setup(job_id: str):
    """Everything the road editor needs: frame size, current/suggested road
    outlines, and thinned vehicle paths to draw underneath them."""
    import json

    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    path = processor.analysis_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=409, detail="This video hasn't been analysed yet.")
    with path.open("r", encoding="utf-8") as fh:
        analysis = json.load(fh)
    return {
        "width": analysis["width"],
        "height": analysis["height"],
        "frame_url": f"/api/videos/{job_id}/frame",
        "road_count": job.get("road_count"),
        "roads": job.get("roads") or analysis["suggested_roads"],
        "skip_stationary": job.get("skip_stationary", True),
        "suggested_roads": analysis["suggested_roads"],
        "trajectories": analysis.get("trajectories", []),
        "colors": roadgeo.ROAD_COLORS_HEX,
        "max_roads": roadgeo.MAX_ROADS,
    }


@app.post("/api/videos/{job_id}/roads")
async def confirm_roads(job_id: str, payload: dict):
    """Save the user's road outlines and render per-road results.

    Body: {"roads": [{"name": str, "polygon": [[x, y], ...]}],
           "skip_stationary": bool (optional, default true)} with x/y
    normalised to 0..1. Works on a video waiting for roads or one that's
    already done (re-render with new outlines - no re-detection needed).
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in RUNNING_STATUSES + ("cancelling", "queued"):
        raise HTTPException(status_code=409, detail="This video is still running — wait for it to finish first.")
    if not processor.tracks_path(job_id).exists():
        raise HTTPException(status_code=409, detail="This video hasn't been analysed yet — click Reprocess.")
    try:
        cleaned = roadgeo.validate_roads((payload or {}).get("roads"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    skip_stationary = (payload or {}).get("skip_stationary", job.get("skip_stationary", True))
    jobs.update_job(
        job_id,
        roads=cleaned,
        road_count=len(cleaned),
        skip_stationary=bool(skip_stationary),
        stage="render",
        status="queued",
        progress=0.0,
        error=None,
    )
    _work_queue.put(job_id)
    return {"ok": True, "status": "queued"}


@app.delete("/api/videos/{job_id}")
async def delete_video(job_id: str):
    """Remove a job and any files it left on disk (upload, output video/stats).

    If the job is actively downloading or processing, this doesn't delete it
    immediately (that would race the worker thread and leave orphaned partial
    files). Instead it requests cancellation: the job's status flips to
    ``cancelling``, and the worker thread — which checks for this on every
    downloaded chunk / processed frame — stops itself, cleans up its own
    partial files, and deletes the job record shortly after.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] in RUNNING_STATUSES:
        jobs.request_cancel(job_id)
        return {"ok": True, "status": "cancelling"}
    if job["status"] == "cancelling":
        return {"ok": True, "status": "cancelling"}

    jobs.delete_job(job_id)
    _cleanup_job_files(job_id)
    return {"ok": True, "status": "deleted"}


@app.post("/api/videos/bulk-delete")
async def bulk_delete_videos(payload: dict):
    """Remove/cancel several jobs at once. Body: {"ids": [job_id, ...]}.

    Each id is handled exactly like a single DELETE call — done/error/queued
    jobs are removed immediately, downloading/processing jobs are cancelled
    (and clean themselves up once the worker notices). Unknown ids are
    reported back rather than raising, so one bad id in a batch doesn't
    block the rest.
    """
    ids = (payload or {}).get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="Provide a non-empty list of video ids under 'ids'.")

    deleted, cancelling, not_found = [], [], []
    for job_id in ids:
        job = jobs.get_job(job_id)
        if job is None:
            not_found.append(job_id)
            continue
        if job["status"] in RUNNING_STATUSES:
            jobs.request_cancel(job_id)
            cancelling.append(job_id)
        elif job["status"] == "cancelling":
            cancelling.append(job_id)
        else:
            jobs.delete_job(job_id)
            _cleanup_job_files(job_id)
            deleted.append(job_id)

    return {"ok": True, "deleted": deleted, "cancelling": cancelling, "not_found": not_found}


def _requeue(job: dict, road_count: int | None = None) -> None:
    """Put a failed/finished job back in the queue.

    - A failure during the render pass (with tracking data still on disk) only
      re-runs the render - no need to detect everything again.
    - Anything else re-runs detection from scratch; previously confirmed road
      outlines are kept and offered again in the editor.
    """
    job_id = job["id"]
    fields: dict[str, Any] = {"progress": 0.0, "error": None}
    if road_count is not None and road_count != job.get("road_count"):
        fields["road_count"] = road_count
        fields["roads"] = None  # different number of roads: start from fresh suggestions

    if (
        job["status"] == "error"
        and job.get("stage") == "render"
        and job.get("roads")
        and processor.tracks_path(job_id).exists()
    ):
        jobs.update_job(job_id, status="queued", stage="render", **fields)
    else:
        stored_name = job.get("stored_name")
        have_file = bool(stored_name) and stored_name != "pending" and (config.UPLOAD_DIR / stored_name).exists()
        status = "downloading" if job.get("source_url") and not have_file else "queued"
        jobs.update_job(job_id, status=status, stage="analyse", **fields)
    _work_queue.put(job_id)


RETRYABLE_STATUSES = ("error", "done", "awaiting_roads")


@app.post("/api/videos/bulk-retry")
async def bulk_retry_videos(payload: dict):
    """Retry several failed/done jobs at once. Body: {"ids": [job_id, ...]}.

    Only jobs currently in ``error`` or ``done`` are retried (same rule as the
    single-job retry endpoint); anything else in the list is skipped and
    reported back, not treated as an error.
    """
    ids = (payload or {}).get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="Provide a non-empty list of video ids under 'ids'.")

    retried, skipped = [], []
    for job_id in ids:
        job = jobs.get_job(job_id)
        if job is None or job["status"] not in RETRYABLE_STATUSES:
            skipped.append(job_id)
            continue
        _requeue(job)
        retried.append(job_id)

    return {"ok": True, "retried": retried, "skipped": skipped}


@app.post("/api/videos/{job_id}/retry")
async def retry_video(job_id: str, payload: dict | None = None):
    """Re-run a job: resume a failed one, or re-detect a finished one.

    Optional body: {"road_count": n} to change how many roads to look for.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in RETRYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="This video can't be retried right now — it's still running.")
    road_count = None
    if payload and payload.get("road_count") not in (None, ""):
        try:
            road_count = _parse_road_count(payload.get("road_count"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    _requeue(job, road_count)
    return {"ok": True, "status": jobs.get_job(job_id)["status"]}


@app.post("/api/videos/{job_id}/trim")
async def trim_video(job_id: str, payload: dict):
    """Cut the job's source video down to [start, end) seconds and reprocess it.

    Works on a video at any stage — including one that's already ``done`` —
    since it trims the original file in ``storage/uploads``, not the annotated
    output. After trimming, the job is reset to ``queued`` and re-run through
    the normal detection pipeline, so its stats/annotated video get replaced
    with results for the shorter clip.

    Body: {"start": <seconds, default 0>, "end": <seconds, optional>,
           "duration": <seconds, optional — alternative to "end">}
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in RUNNING_STATUSES + ("cancelling", "queued"):
        raise HTTPException(
            status_code=409,
            detail="This video is still running — wait for it to finish or fail before trimming.",
        )

    stored_name = job.get("stored_name")
    if not stored_name or stored_name == "pending" or not (config.UPLOAD_DIR / stored_name).exists():
        raise HTTPException(status_code=400, detail="No source file to trim yet for this job.")

    payload = payload or {}
    try:
        start = float(payload.get("start", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'start' must be a number.")

    end = payload.get("end")
    duration = payload.get("duration")
    try:
        if duration is not None:
            end = start + float(duration)
        elif end is not None:
            end = float(end)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'end'/'duration' must be numbers.")

    if end is None and start <= 0:
        raise HTTPException(
            status_code=400,
            detail="Specify a start after 0, an end time, or a duration to trim to.",
        )

    try:
        new_stored_name = trimmer.trim_source(job_id, stored_name, start, end)
    except trimmer.TrimError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Drop every output from the previous run (videos, stats, tracking data):
    # they describe footage that no longer exists. Road outlines are kept in
    # the job record and offered again once the shorter clip is analysed.
    for stale in config.OUTPUT_DIR.glob(f"{job_id}*"):
        try:
            stale.unlink()
        except OSError:
            pass

    jobs.update_job(
        job_id,
        stored_name=new_stored_name,
        status="queued",
        stage="analyse",
        progress=0.0,
        error=None,
        summary=None,
    )
    _work_queue.put(job_id)

    return {"ok": True, "status": "queued", "start": start, "end": end}


# ---------------------------------------------------------------------------
# Frontend (mounted last so it doesn't shadow /api routes)
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    index_file = config.FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "frontend not found"}, status_code=500)
    return FileResponse(str(index_file))


app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")
