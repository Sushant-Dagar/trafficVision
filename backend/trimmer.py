"""Trim the *source* video behind an existing job, even after it's been processed.

This is separate from ``downloader._trim_with_ffmpeg`` (which trims a freshly
downloaded remote video to a max length). Here the user picks an explicit
start/end on a video that's already sitting in ``storage/uploads`` — possibly one
that's already been detected+annotated once — and we cut it down and hand it
back to the normal processing pipeline to re-run.

Public API:
    trim_source(job_id, start, end) -> new_stored_name
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config


class TrimError(RuntimeError):
    """Raised when ffmpeg fails or arguments don't make sense."""


def _ffmpeg_exe() -> str:
    if config.FFMPEG_EXE and Path(config.FFMPEG_EXE).exists():
        return config.FFMPEG_EXE
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise TrimError("No ffmpeg binary available for trimming.") from exc


def trim_source(job_id: str, stored_name: str, start: float, end: float | None) -> str:
    """Cut ``storage/uploads/<stored_name>`` down to [start, end) seconds.

    Re-encodes to H.264 mp4 (consistent with everything else the app writes),
    drops audio (detection doesn't need it), and replaces the job's stored file.
    Returns the new stored filename (always ``<job_id>.mp4``).

    Raises TrimError on bad input or if ffmpeg fails.
    """
    if start < 0:
        raise TrimError("Start time can't be negative.")
    if end is not None and end <= start:
        raise TrimError("End time must be after the start time.")

    src = config.UPLOAD_DIR / stored_name
    if not src.exists():
        raise TrimError(f"Source video not found: {stored_name}")

    dst = config.UPLOAD_DIR / f"{job_id}.trim.mp4"

    ff = _ffmpeg_exe()
    cmd = [
        ff, "-y",
        "-ss", str(start),
        "-i", str(src),
    ]
    if end is not None:
        cmd += ["-t", str(round(end - start, 3))]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",                # drop audio, matches the rest of the pipeline
        "-movflags", "+faststart",
        str(dst),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        tail = (proc.stderr or "")[-500:]
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise TrimError(f"ffmpeg trim failed: {tail}")

    # Swap the trimmed file in as the job's new source. If the old file used a
    # different extension (e.g. a .mov upload), drop it and standardise on mp4.
    final_name = f"{job_id}.mp4"
    final_path = config.UPLOAD_DIR / final_name
    if src.resolve() != final_path.resolve() and src.exists():
        try:
            src.unlink()
        except OSError:
            pass
    os.replace(dst, final_path)

    return final_name
