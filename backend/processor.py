"""Video processing pipeline: detect + track vehicles, annotate, encode, aggregate.

Called from a background thread (see main.py). For a given job it:
  1. reads the uploaded video frame by frame,
  2. runs YOLO detection + ByteTrack tracking so every vehicle gets a stable id,
  3. draws boxes/labels and writes an annotated, browser-playable H.264 MP4,
  4. majority-votes each track's class across all frames it appears in,
  5. aggregates per-type / per-category counts and writes a stats JSON.

The heavy imports (ultralytics/torch/opencv) are done lazily so importing this
module (e.g. by the web app at startup) stays cheap and fast.
"""
from __future__ import annotations

import json
import traceback
from collections import Counter, defaultdict
from typing import Any

from . import config, jobs

# The YOLO model is expensive to construct, so load it once and reuse it across
# every job in this process.
_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from ultralytics import YOLO  # lazy import

        _MODEL = YOLO(config.MODEL_NAME)
    return _MODEL


def _label_for(class_id: int) -> dict[str, Any]:
    """COCO class id -> our vehicle descriptor (name/category/wheels/color)."""
    return config.VEHICLE_CLASSES.get(
        class_id,
        {"name": "vehicle", "category": "unknown", "wheels": 0, "color": (200, 200, 200)},
    )


def _draw_box(frame, xyxy, text: str, color: tuple[int, int, int]) -> None:
    """Draw a bounding box with a filled label tag onto a BGR frame in place."""
    import cv2

    x1, y1, x2, y2 = (int(v) for v in xyxy)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

    # Label background for readability.
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    ty = max(y1, th + 4)
    cv2.rectangle(frame, (x1, ty - th - 4), (x1 + tw + 4, ty + baseline - 2), color, -1)
    cv2.putText(
        frame, text, (x1 + 2, ty - 2),
        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
    )


def _even(n: int) -> int:
    """Round down to an even number (yuv420p requires even width/height)."""
    return n - (n % 2)


# ---------------------------------------------------------------------------
# Road / lane layout estimate
# ---------------------------------------------------------------------------
# Not a geometric lane-marking detector — instead it clusters the direction each
# vehicle travels (first-seen position -> last-seen position) to find the
# intersection's dominant traffic-flow directions ("roads"), then, within each
# direction, clusters vehicles' entry position across the road's width to guess
# how many lanes it carries. It's a heuristic read on movement patterns, so it
# works best on clips with a reasonable amount of steady, mostly-straight
# traffic, and is labelled as an estimate wherever it's shown.
def _cluster_1d_linear(values: list[float], tolerance: float) -> list[list[float]]:
    if not values:
        return []
    values = sorted(values)
    clusters = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return clusters


def _cluster_circular(values: list[float], period: float, tolerance: float) -> list[list[float]]:
    """Cluster angular values (degrees) on a circle of the given period."""
    if not values:
        return []
    values = sorted(v % period for v in values)
    n = len(values)
    if n == 1:
        return [values]
    gaps = [(values[(i + 1) % n] - values[i]) % period for i in range(n)]
    cut = gaps.index(max(gaps))
    ordered = values[cut + 1 :] + [v + period for v in values[: cut + 1]]
    clusters = [[ordered[0]]]
    for v in ordered[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [[v % period for v in c] for c in clusters]


def _estimate_road_layout(
    track_first_pos: dict[int, tuple[float, float]],
    track_last_pos: dict[int, tuple[float, float]],
    valid_tids: set[int],
    frame_w: int,
    frame_h: int,
) -> dict[str, Any]:
    import math

    diag = math.hypot(frame_w, frame_h)
    min_displacement = max(20.0, diag * 0.06)

    samples = []
    for tid in valid_tids:
        first = track_first_pos.get(tid)
        last = track_last_pos.get(tid)
        if not first or not last:
            continue
        dx, dy = last[0] - first[0], last[1] - first[1]
        dist = math.hypot(dx, dy)
        if dist < min_displacement:
            continue  # too little movement to trust a direction from
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        samples.append({"angle": angle, "first": first})

    if len(samples) < 3:
        return {
            "arms_estimate": None,
            "roads_estimate": None,
            "directions": [],
            "vehicles_used": 0,
            "vehicles_total": len(valid_tids),
            "note": "Not enough moving vehicles in this clip to estimate the road layout.",
        }

    angle_tolerance = 28.0
    angle_clusters = _cluster_circular([s["angle"] for s in samples], 360.0, angle_tolerance)

    def circular_mean(angles_deg: list[float]) -> float:
        sx = sum(math.sin(math.radians(a)) for a in angles_deg)
        cx = sum(math.cos(math.radians(a)) for a in angles_deg)
        return math.degrees(math.atan2(sx, cx)) % 360.0

    centers = [circular_mean(c) for c in angle_clusters]
    groups: list[list[dict]] = [[] for _ in centers]
    for s in samples:
        best = min(
            range(len(centers)),
            key=lambda i: min(abs(s["angle"] - centers[i]), 360 - abs(s["angle"] - centers[i])),
        )
        groups[best].append(s)

    lane_tolerance = max(18.0, diag * 0.035)
    directions_out = []
    for center, group in zip(centers, groups):
        if len(group) < 2:
            continue  # a single stray track isn't enough to call it a road
        theta = math.radians(center)
        perp = (-math.sin(theta), math.cos(theta))
        offsets = [s["first"][0] * perp[0] + s["first"][1] * perp[1] for s in group]
        lane_clusters = _cluster_1d_linear(offsets, lane_tolerance)
        directions_out.append(
            {
                "heading_deg": round(center, 1),
                "vehicle_count": len(group),
                "lanes_estimate": len(lane_clusters),
                "lane_offsets": [sum(c) / len(c) for c in lane_clusters],
            }
        )

    vehicles_used = sum(d["vehicle_count"] for d in directions_out)

    # Two directions roughly 180 degrees apart are the two sides of one road
    # (traffic going each way) rather than two separate roads, so merge them.
    used: set[int] = set()
    roads = 0
    for i in range(len(directions_out)):
        if i in used:
            continue
        used.add(i)
        roads += 1
        hi = directions_out[i]["heading_deg"]
        for j in range(len(directions_out)):
            if j in used:
                continue
            hj = directions_out[j]["heading_deg"]
            diff = abs(hi - hj)
            diff = min(diff, 360 - diff)
            if abs(diff - 180) <= 35:
                used.add(j)
                break

    return {
        "arms_estimate": len(directions_out) or None,
        "roads_estimate": roads if directions_out else None,
        "directions": sorted(directions_out, key=lambda d: -d["vehicle_count"]),
        "vehicles_used": vehicles_used,
        "vehicles_total": len(valid_tids),
        "note": "Estimated from vehicle movement patterns in this clip — treat as approximate, not a survey-grade count.",
    }


# BGR colors (OpenCV order), matched to the frontend's yellow/blue/green/red/etc
# accent palette, with two extras in case a clip has more than four approaches.
_ROAD_PALETTE_BGR = [
    (31, 178, 243),   # yellow
    (184, 132, 74),   # blue
    (108, 173, 69),   # green
    (70, 86, 225),    # red
    (200, 120, 200),  # magenta (extra)
    (150, 200, 90),   # teal-green (extra)
]


def _compass(deg: float) -> str:
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    return dirs[round((deg % 360) / 45) % 8]


def _draw_road_overlay(frame, road_layout: dict[str, Any]):
    """Draw the estimated lane lines for each approach direction onto a copy of
    ``frame``, with a small label per direction. Returns the annotated frame."""
    import math

    import cv2

    h, w = frame.shape[:2]
    diag = math.hypot(w, h)
    overlay = frame.copy()

    for i, d in enumerate(road_layout.get("directions", [])):
        color = _ROAD_PALETTE_BGR[i % len(_ROAD_PALETTE_BGR)]
        theta = math.radians(d["heading_deg"])
        dirv = (math.cos(theta), math.sin(theta))
        perp = (-math.sin(theta), math.cos(theta))
        lane_offsets = d.get("lane_offsets") or []

        for off in lane_offsets:
            p1 = (int(-diag * dirv[0] + off * perp[0]), int(-diag * dirv[1] + off * perp[1]))
            p2 = (int(diag * dirv[0] + off * perp[0]), int(diag * dirv[1] + off * perp[1]))
            cv2.line(overlay, p1, p2, color, 3, cv2.LINE_AA)

        if lane_offsets:
            mean_off = sum(lane_offsets) / len(lane_offsets)
            t_frac = 0.12 + 0.1 * (i % 3)
            lx = int(t_frac * diag * dirv[0] + mean_off * perp[0])
            ly = int(t_frac * diag * dirv[1] + mean_off * perp[1])
            lx = min(max(lx, 8), max(8, w - 150))
            ly = min(max(ly, 22), max(22, h - 10))
            n_lanes = len(lane_offsets)
            label = f"{_compass(d['heading_deg'])} - {n_lanes} lane{'s' if n_lanes != 1 else ''}"
            (tw, th), _base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            cv2.rectangle(overlay, (lx - 4, ly - th - 6), (lx + tw + 4, ly + 4), color, -1)
            cv2.putText(
                overlay, label, (lx, ly),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 25), 2, cv2.LINE_AA,
            )

    # Slightly transparent lines/labels over the original frame, rather than fully opaque.
    return cv2.addWeighted(overlay, 0.8, frame, 0.2, 0)


class _Cancelled(Exception):
    """Internal signal: the job was cancelled mid-processing (see jobs.is_cancelled)."""


def process_video(job_id: str) -> None:
    """Full pipeline for one job. Updates job status/progress as it goes."""
    import cv2
    import imageio.v2 as imageio
    import numpy as np  # noqa: F401  (kept explicit; used indirectly by cv2/imageio)

    job = jobs.get_job(job_id)
    if job is None:
        return
    if jobs.is_cancelled(job_id):
        jobs.delete_job(job_id)
        return

    src_path = config.UPLOAD_DIR / job["stored_name"]
    out_video_path = config.OUTPUT_DIR / f"{job_id}.mp4"
    out_stats_path = config.OUTPUT_DIR / f"{job_id}.json"

    jobs.update_job(job_id, status="processing", progress=0.0)

    writer = None
    cap = None
    try:
        model = _get_model()

        cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("Could not open the uploaded video (unsupported/corrupt file).")

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0 or fps > 240:
            fps = config.DEFAULT_FPS
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        out_w, out_h = _even(src_w), _even(src_h)

        # Browser-playable H.264 via imageio's bundled ffmpeg (imageio-ffmpeg).
        writer = imageio.get_writer(
            str(out_video_path),
            format="ffmpeg",
            fps=fps,
            codec="libx264",
            pixelformat="yuv420p",
            quality=7,
            macro_block_size=1,  # we already forced even dims; don't auto-resize
        )

        # Per-track accumulators.
        track_class_votes: dict[int, Counter] = defaultdict(Counter)
        track_conf_sum: dict[int, float] = defaultdict(float)
        track_frame_count: dict[int, int] = defaultdict(int)
        track_first_pos: dict[int, tuple[float, float]] = {}
        track_last_pos: dict[int, tuple[float, float]] = {}

        # A clean (no boxes drawn) mid-video frame, used later as the base image
        # for the road/lane overlay snapshot.
        snapshot_frame = None
        snapshot_target_idx = max(1, total_frames // 2) if total_frames else 60

        frame_idx = 0
        while True:
            if jobs.is_cancelled(job_id):
                raise _Cancelled()

            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            # Crop to even dimensions so the encoded size matches the writer.
            if frame.shape[1] != out_w or frame.shape[0] != out_h:
                frame = frame[:out_h, :out_w]

            if snapshot_frame is None and frame_idx >= snapshot_target_idx:
                snapshot_frame = frame.copy()

            results = model.track(
                frame,
                persist=True,
                tracker=config.TRACKER,
                classes=config.TARGET_CLASS_IDS,
                conf=config.CONF_THRESHOLD,
                imgsz=config.IMG_SIZE,
                verbose=False,
            )

            boxes = results[0].boxes if results else None
            if boxes is not None and boxes.id is not None:
                ids = boxes.id.int().cpu().tolist()
                clss = boxes.cls.int().cpu().tolist()
                confs = boxes.conf.cpu().tolist()
                xyxys = boxes.xyxy.cpu().tolist()

                for tid, cid, conf, xyxy in zip(ids, clss, confs, xyxys):
                    track_class_votes[tid][cid] += 1
                    track_conf_sum[tid] += float(conf)
                    track_frame_count[tid] += 1

                    centroid = ((xyxy[0] + xyxy[2]) / 2.0, (xyxy[1] + xyxy[3]) / 2.0)
                    if tid not in track_first_pos:
                        track_first_pos[tid] = centroid
                    track_last_pos[tid] = centroid

                    desc = _label_for(cid)
                    _draw_box(frame, xyxy, f"{desc['name']} #{tid}", desc["color"])

            # imageio expects RGB.
            writer.append_data(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            # Throttle progress writes to ~ every 20 frames.
            if total_frames and frame_idx % 20 == 0:
                jobs.update_job(job_id, progress=min(0.99, frame_idx / total_frames))

        writer.close()
        writer = None
        cap.release()
        cap = None

        summary = _aggregate(track_class_votes, track_conf_sum, track_frame_count)
        summary["frames"] = frame_idx
        summary["fps"] = round(fps, 2)
        summary["resolution"] = f"{out_w}x{out_h}"

        valid_tids = {v["id"] for v in summary["vehicles"]}
        summary["road_layout"] = _estimate_road_layout(
            track_first_pos, track_last_pos, valid_tids, out_w, out_h
        )

        # Best-effort visual snapshot of the estimated roads/lanes — never let a
        # problem here fail the whole job, since the main results are already done.
        if snapshot_frame is not None and summary["road_layout"].get("directions"):
            try:
                roadmap = _draw_road_overlay(snapshot_frame, summary["road_layout"])
                out_roadmap_path = config.OUTPUT_DIR / f"{job_id}_roadmap.jpg"
                cv2.imwrite(str(out_roadmap_path), roadmap, [cv2.IMWRITE_JPEG_QUALITY, 90])
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        with out_stats_path.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)

        jobs.update_job(
            job_id,
            status="done",
            progress=1.0,
            summary={
                "total": summary["total"],
                "two_wheelers": summary["categories"].get("2-wheeler", 0),
                "four_wheelers": summary["categories"].get("4-wheeler", 0),
            },
        )
    except _Cancelled:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if cap is not None:
            cap.release()
        for path in (out_video_path, out_stats_path, config.OUTPUT_DIR / f"{job_id}_roadmap.jpg"):
            try:
                path.unlink()
            except OSError:
                pass
        jobs.delete_job(job_id)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if cap is not None:
            cap.release()
        jobs.update_job(
            job_id,
            status="error",
            error=f"{exc}",
        )
        # Also log to the server console for debugging.
        traceback.print_exc()


def _aggregate(
    track_class_votes: dict[int, Counter],
    track_conf_sum: dict[int, float],
    track_frame_count: dict[int, int],
) -> dict[str, Any]:
    """Turn per-track observations into the final stats structure."""
    vehicles: list[dict[str, Any]] = []
    per_type: Counter = Counter()
    per_category: Counter = Counter()

    for tid, votes in track_class_votes.items():
        n_frames = track_frame_count[tid]
        if n_frames < config.MIN_TRACK_FRAMES:
            continue  # too fleeting to trust — likely a false positive

        class_id = votes.most_common(1)[0][0]  # majority vote stabilises flicker
        desc = _label_for(class_id)
        avg_conf = track_conf_sum[tid] / max(1, n_frames)

        vehicles.append(
            {
                "id": int(tid),
                "type": desc["name"],
                "category": desc["category"],
                "wheels": desc["wheels"],
                "confidence": round(avg_conf, 3),
                "frames_seen": n_frames,
            }
        )
        per_type[desc["name"]] += 1
        per_category[desc["category"]] += 1

    vehicles.sort(key=lambda v: v["id"])

    return {
        "total": len(vehicles),
        "by_type": dict(per_type),
        "categories": dict(per_category),
        "vehicles": vehicles,
    }
