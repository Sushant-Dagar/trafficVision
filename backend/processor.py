"""Video processing pipeline, in two passes.

Pass 1 - ``analyse_video`` (the slow part, runs YOLO):
  1. reads the video frame by frame,
  2. runs YOLO detection + ByteTrack tracking so every vehicle gets a stable id,
  3. saves every detection to ``<job>_tracks.npy`` and a clean reference frame
     to ``<job>_frame.jpg``,
  4. suggests one outline per road (see roads.py) and stops in the
     ``awaiting_roads`` state so the user can adjust the outlines.

Pass 2 - ``render_job`` (fast, no YOLO - reuses the saved tracks):
  1. assigns each vehicle to the road it came from,
  2. writes an annotated overview video of the whole junction plus one video
     cropped to each road,
  3. writes per-road counts and a timeline of when each vehicle arrived.

Because pass 2 only needs the saved tracks, the user can re-edit the road
outlines on a finished video and get new results in seconds.

Heavy imports (ultralytics/torch/opencv) are done lazily so importing this
module (e.g. by the web app at startup) stays cheap.
"""
from __future__ import annotations

import json
import math
import traceback
from collections import Counter
from typing import Any

import numpy as np

from . import config, jobs, roads as roadgeo

_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from ultralytics import YOLO  # lazy import

        _MODEL = YOLO(config.MODEL_NAME)
    return _MODEL


def _reset_tracker(model) -> None:
    """Start each video with fresh track ids (``persist=True`` would otherwise
    carry the previous video's tracks over into this one)."""
    predictor = getattr(model, "predictor", None)
    for tracker in getattr(predictor, "trackers", None) or []:
        try:
            tracker.reset()
        except Exception:  # noqa: BLE001
            pass


def _label_for(class_id: int) -> dict[str, Any]:
    return config.VEHICLE_CLASSES.get(
        class_id,
        {"name": "vehicle", "category": "unknown", "wheels": 0, "color": (200, 200, 200)},
    )


def _even(n: int) -> int:
    return n - (n % 2)


def tracks_path(job_id: str):
    return config.OUTPUT_DIR / f"{job_id}_tracks.npy"


def frame_path(job_id: str):
    return config.OUTPUT_DIR / f"{job_id}_frame.jpg"


def analysis_path(job_id: str):
    return config.OUTPUT_DIR / f"{job_id}_analysis.json"


def road_video_path(job_id: str, index: int):
    return config.OUTPUT_DIR / f"{job_id}_road{index}.mp4"


class _Cancelled(Exception):
    """Internal signal: the job was cancelled mid-processing."""


def _cleanup_outputs(job_id: str) -> None:
    """Remove everything a cancelled job left on disk (outputs and its upload)."""
    for path in [*config.OUTPUT_DIR.glob(f"{job_id}*"), *config.UPLOAD_DIR.glob(f"{job_id}*")]:
        try:
            path.unlink()
        except OSError:
            pass


def _open_writer(path, fps: float):
    import imageio.v2 as imageio

    return imageio.get_writer(
        str(path),
        format="ffmpeg",
        fps=fps,
        codec="libx264",
        pixelformat="yuv420p",
        quality=7,
        macro_block_size=1,  # dims are already even; don't let imageio resize
        ffmpeg_params=["-preset", "veryfast", "-movflags", "+faststart"],
    )


# ===========================================================================
# Pass 1: detect + track, then suggest roads
# ===========================================================================
def analyse_video(job_id: str) -> None:
    import cv2

    job = jobs.get_job(job_id)
    if job is None:
        return
    if jobs.is_cancelled(job_id):
        jobs.delete_job(job_id)
        return

    src_path = config.UPLOAD_DIR / job["stored_name"]
    jobs.update_job(job_id, status="processing", progress=0.0, stage="analyse", error=None)

    cap = None
    try:
        model = _get_model()
        _reset_tracker(model)

        cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("Could not open the uploaded video (unsupported/corrupt file).")

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0 or fps > 240:
            fps = config.DEFAULT_FPS
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        w = _even(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
        h = _even(int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

        # A clean frame ~1s in (skips fade-ins) for the road editor background.
        ref_idx = max(1, min(int(round(fps)), total_frames or int(round(fps))))
        ref_frame = None

        rows: list[list[float]] = []
        frame_idx = 0
        while True:
            if jobs.is_cancelled(job_id):
                raise _Cancelled()
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if frame.shape[1] != w or frame.shape[0] != h:
                frame = frame[:h, :w]
            if ref_frame is None and frame_idx >= ref_idx:
                ref_frame = frame.copy()

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
                for tid, cid, conf, (x1, y1, x2, y2) in zip(ids, clss, confs, xyxys):
                    rows.append([frame_idx, tid, cid, conf, x1, y1, x2, y2])

            if total_frames and frame_idx % 20 == 0:
                jobs.update_job(job_id, progress=min(0.99, frame_idx / total_frames))

        cap.release()
        cap = None
        if frame_idx == 0:
            raise RuntimeError("The video has no readable frames.")
        if ref_frame is None:
            # Very short clip: grab the first frame instead.
            cap = cv2.VideoCapture(str(src_path))
            ok, ref_frame = cap.read()
            cap.release()
            cap = None
            ref_frame = ref_frame[:h, :w] if ok else np.zeros((h, w, 3), np.uint8)

        arr = np.asarray(rows, dtype=np.float32).reshape(-1, 8)
        np.save(tracks_path(job_id), arr)
        cv2.imwrite(str(frame_path(job_id)), ref_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])

        tracks = roadgeo.group_tracks(arr)
        n_roads = int(job.get("road_count") or 4)
        suggested = roadgeo.suggest_roads(tracks, w, h, n_roads, config.MIN_TRACK_FRAMES)
        with analysis_path(job_id).open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "width": w,
                    "height": h,
                    "fps": round(float(fps), 3),
                    "frames": frame_idx,
                    "suggested_roads": suggested,
                    "trajectories": roadgeo.trajectories_for_display(
                        tracks, w, h, config.MIN_TRACK_FRAMES
                    ),
                },
                fh,
            )

        jobs.update_job(
            job_id,
            status="awaiting_roads",
            progress=1.0,
            stage="analyse",
            video_meta={"width": w, "height": h, "fps": float(fps), "frames": frame_idx},
            suggested_roads=suggested,
        )
    except _Cancelled:
        if cap is not None:
            cap.release()
        _cleanup_outputs(job_id)
        jobs.delete_job(job_id)
    except Exception as exc:  # noqa: BLE001
        if cap is not None:
            cap.release()
        jobs.update_job(job_id, status="error", error=f"{exc}", stage="analyse")
        traceback.print_exc()


# ===========================================================================
# Pass 2: assign roads, render per-road videos, build stats
# ===========================================================================
def _vehicle_summary(tracks: dict[int, np.ndarray]) -> dict[int, dict[str, Any]]:
    """Majority-vote class + average confidence for every trustworthy track."""
    out = {}
    for tid, chunk in tracks.items():
        if len(chunk) < config.MIN_TRACK_FRAMES:
            continue  # too fleeting to trust - likely a false positive
        votes = Counter(chunk[:, roadgeo.COL_CLS].astype(int).tolist())
        class_id = votes.most_common(1)[0][0]
        desc = _label_for(class_id)
        out[tid] = {
            "id": int(tid),
            "type": desc["name"],
            "category": desc["category"],
            "wheels": desc["wheels"],
            "confidence": round(float(chunk[:, roadgeo.COL_CONF].mean()), 3),
            "frames_seen": int(len(chunk)),
            "first_frame": int(chunk[0, roadgeo.COL_FRAME]),
            "color": desc["color"],
        }
    return out


def _crop_rect(poly: np.ndarray, w: int, h: int) -> tuple[int, int, int, int]:
    """Padded bounding box of a road outline, with even width/height."""
    pad = int(0.04 * math.hypot(w, h))
    x1 = max(0, int(poly[:, 0].min()) - pad)
    y1 = max(0, int(poly[:, 1].min()) - pad)
    x2 = min(w, int(math.ceil(poly[:, 0].max())) + pad)
    y2 = min(h, int(math.ceil(poly[:, 1].max())) + pad)
    min_side = 96
    if x2 - x1 < min_side:
        cx = (x1 + x2) // 2
        x1, x2 = max(0, cx - min_side // 2), min(w, cx + min_side // 2)
    if y2 - y1 < min_side:
        cy = (y1 + y2) // 2
        y1, y2 = max(0, cy - min_side // 2), min(h, cy + min_side // 2)
    cw, ch = _even(x2 - x1), _even(y2 - y1)
    return x1, y1, cw, ch


def _draw_box(frame, xyxy, text: str | None, color, thickness: int = 2) -> None:
    import cv2

    x1, y1, x2, y2 = (int(v) for v in xyxy)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
    if not text:
        return
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    ty = max(y1, th + 4)
    cv2.rectangle(frame, (x1, ty - th - 4), (x1 + tw + 4, ty + baseline - 2), color, -1)
    cv2.putText(frame, text, (x1 + 2, ty - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (20, 20, 20), 1, cv2.LINE_AA)


def _draw_tag(frame, text: str, org, color) -> None:
    """Filled label with dark text (road names, live counters)."""
    import cv2

    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
    x, y = int(org[0]), int(org[1])
    x = min(max(x, 4), max(4, frame.shape[1] - tw - 10))
    y = min(max(y, th + 8), max(th + 8, frame.shape[0] - 6))
    cv2.rectangle(frame, (x - 4, y - th - 6), (x + tw + 4, y + 5), color, -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2, cv2.LINE_AA)


def render_job(job_id: str) -> None:
    import cv2

    job = jobs.get_job(job_id)
    if job is None:
        return
    if jobs.is_cancelled(job_id):
        jobs.delete_job(job_id)
        return

    road_defs = job.get("roads") or []
    meta = job.get("video_meta") or {}
    src_path = config.UPLOAD_DIR / job["stored_name"]
    jobs.update_job(job_id, status="rendering", progress=0.0, stage="render", error=None)

    cap = None
    writers: list = []
    try:
        if not road_defs:
            raise RuntimeError("No roads have been marked for this video yet.")
        if not tracks_path(job_id).exists():
            raise RuntimeError("The vehicle tracking data is missing - click Reprocess to re-detect.")

        w, h = int(meta["width"]), int(meta["height"])
        fps = float(meta.get("fps") or config.DEFAULT_FPS)
        total_frames = int(meta.get("frames") or 0)

        tracks = roadgeo.group_tracks(np.load(tracks_path(job_id)))
        vehicles = _vehicle_summary(tracks)
        valid_tracks = {tid: tracks[tid] for tid in vehicles}
        assignment = roadgeo.assign_roads(
            valid_tracks, road_defs, w, h, max_delay_frames=max(3, int(round(fps)))
        )

        # Per-frame lookup of the detections to draw.
        all_rows = np.concatenate(list(valid_tracks.values())) if valid_tracks else np.zeros((0, 8), np.float32)
        all_rows = all_rows[np.argsort(all_rows[:, roadgeo.COL_FRAME], kind="stable")]
        frame_nums = all_rows[:, roadgeo.COL_FRAME].astype(np.int64)
        starts = np.searchsorted(frame_nums, np.arange(total_frames + 2), side="left")

        # Road geometry, colours, crops and masks.
        polys = [roadgeo.denormalise(r["polygon"], w, h) for r in road_defs]
        colors = [roadgeo.road_color_bgr(i) for i in range(len(road_defs))]
        crops = [_crop_rect(p, w, h) for p in polys]
        fill_layer = np.zeros((h, w, 3), np.uint8)
        any_road = np.zeros((h, w), np.uint8)
        crop_masks = []
        for i, p in enumerate(polys):
            pi = p.astype(np.int32)
            cv2.fillPoly(fill_layer, [pi], colors[i])
            cv2.fillPoly(any_road, [pi], 255)
            x, y, cw, ch = crops[i]
            m = np.zeros((h, w), np.uint8)
            cv2.fillPoly(m, [pi], 255)
            crop_masks.append(m[y:y + ch, x:x + cw] > 0)
        any_road = any_road > 0

        # Arrival frame per vehicle -> running counters drawn on the videos.
        arrivals_by_road: list[list[int]] = [[] for _ in road_defs]
        for tid, (road, frame_no) in assignment.items():
            arrivals_by_road[road].append(frame_no)
        for lst in arrivals_by_road:
            lst.sort()

        overview_writer = _open_writer(config.OUTPUT_DIR / f"{job_id}.mp4", fps)
        writers.append(overview_writer)
        road_writers = []
        for i in range(len(road_defs)):
            wr = _open_writer(road_video_path(job_id, i), fps)
            writers.append(wr)
            road_writers.append(wr)

        cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("Could not reopen the source video.")

        frame_idx = 0
        while True:
            if jobs.is_cancelled(job_id):
                raise _Cancelled()
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if total_frames and frame_idx > total_frames:
                break
            if frame.shape[1] != w or frame.shape[0] != h:
                frame = frame[:h, :w]

            lo = starts[frame_idx] if frame_idx < len(starts) else len(all_rows)
            hi = starts[frame_idx + 1] if frame_idx + 1 < len(starts) else len(all_rows)
            dets = all_rows[lo:hi]

            # --- overview: tinted road outlines + every vehicle coloured by road
            over = frame.copy()
            blended = cv2.addWeighted(over, 0.8, fill_layer, 0.2, 0)
            np.copyto(over, blended, where=any_road[:, :, None])
            for i, p in enumerate(polys):
                cv2.polylines(over, [p.astype(np.int32)], True, colors[i], 2, cv2.LINE_AA)
            for d in dets:
                tid = int(d[roadgeo.COL_TID])
                v = vehicles[tid]
                road = assignment.get(tid)
                color = colors[road[0]] if road else (170, 170, 170)
                tag = f"{v['type']} #{tid}" + (f" R{road[0] + 1}" if road else "")
                _draw_box(over, d[roadgeo.COL_X1:roadgeo.COL_Y2 + 1], tag, color)
            for i, p in enumerate(polys):
                count = int(np.searchsorted(arrivals_by_road[i], frame_idx, side="right"))
                cx, cy = p.mean(axis=0)
                _draw_tag(over, f"{road_defs[i]['name']}: {count}", (cx - 40, cy), colors[i])
            overview_writer.append_data(cv2.cvtColor(over, cv2.COLOR_BGR2RGB))

            # --- one cropped video per road
            for i, (x, y, cw, ch) in enumerate(crops):
                crop = frame[y:y + ch, x:x + cw].copy()
                outside = ~crop_masks[i]
                crop[outside] = (crop[outside] * 0.4).astype(np.uint8)  # dim everything off this road
                cv2.polylines(crop, [(polys[i] - [x, y]).astype(np.int32)], True, colors[i], 2, cv2.LINE_AA)
                for d in dets:
                    tid = int(d[roadgeo.COL_TID])
                    box = d[roadgeo.COL_X1:roadgeo.COL_Y2 + 1] - np.array([x, y, x, y], dtype=np.float32)
                    if box[2] < 0 or box[3] < 0 or box[0] > cw or box[1] > ch:
                        continue
                    road = assignment.get(tid)
                    if road and road[0] == i:
                        v = vehicles[tid]
                        _draw_box(crop, box, f"{v['type']} #{tid}", colors[i])
                    else:
                        _draw_box(crop, box, None, (150, 150, 150), 1)
                count = int(np.searchsorted(arrivals_by_road[i], frame_idx, side="right"))
                _draw_tag(crop, f"{road_defs[i]['name']}: {count}", (10, 26), colors[i])
                road_writers[i].append_data(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

            if total_frames and frame_idx % 20 == 0:
                jobs.update_job(job_id, progress=min(0.99, frame_idx / total_frames))

        for wr in writers:
            wr.close()
        writers = []
        cap.release()
        cap = None

        stats = _build_stats(job_id, road_defs, vehicles, assignment, crops, w, h, fps, frame_idx)
        with (config.OUTPUT_DIR / f"{job_id}.json").open("w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2)

        jobs.update_job(
            job_id,
            status="done",
            progress=1.0,
            stage="render",
            summary={
                "total": stats["total"],
                "two_wheelers": stats["categories"].get("2-wheeler", 0),
                "four_wheelers": stats["categories"].get("4-wheeler", 0),
                "roads": [
                    {"name": r["name"], "total": r["total"], "color": r["color"]}
                    for r in stats["roads"]
                ],
            },
        )
    except _Cancelled:
        for wr in writers:
            try:
                wr.close()
            except Exception:  # noqa: BLE001
                pass
        if cap is not None:
            cap.release()
        _cleanup_outputs(job_id)
        jobs.delete_job(job_id)
    except Exception as exc:  # noqa: BLE001
        for wr in writers:
            try:
                wr.close()
            except Exception:  # noqa: BLE001
                pass
        if cap is not None:
            cap.release()
        jobs.update_job(job_id, status="error", error=f"{exc}", stage="render")
        traceback.print_exc()


def _build_stats(job_id, road_defs, vehicles, assignment, crops, w, h, fps, frames) -> dict[str, Any]:
    per_type: Counter = Counter()
    per_cat: Counter = Counter()
    road_stats = []
    for i, r in enumerate(road_defs):
        x, y, cw, ch = crops[i]
        road_stats.append(
            {
                "index": i,
                "name": r["name"],
                "color": roadgeo.road_color_hex(i),
                "polygon": r["polygon"],
                "crop": [x, y, cw, ch],
                "video_url": f"/api/videos/{job_id}/roads/{i}/video",
                "total": 0,
                "by_type": Counter(),
                "categories": Counter(),
                "events": [],
            }
        )

    vehicle_rows = []
    for tid, v in sorted(vehicles.items()):
        per_type[v["type"]] += 1
        per_cat[v["category"]] += 1
        road = assignment.get(tid)
        row = {k: v[k] for k in ("id", "type", "category", "wheels", "confidence", "frames_seen")}
        row["road"] = road[0] if road else None
        row["t"] = round((road[1] if road else v["first_frame"]) / fps, 2)
        vehicle_rows.append(row)
        if road:
            rs = road_stats[road[0]]
            rs["total"] += 1
            rs["by_type"][v["type"]] += 1
            rs["categories"][v["category"]] += 1
            rs["events"].append(
                {"t": row["t"], "id": v["id"], "type": v["type"], "category": v["category"]}
            )

    for rs in road_stats:
        rs["by_type"] = dict(rs["by_type"])
        rs["categories"] = dict(rs["categories"])
        rs["events"].sort(key=lambda e: e["t"])

    return {
        "total": len(vehicle_rows),
        "by_type": dict(per_type),
        "categories": dict(per_cat),
        "unassigned": sum(1 for v in vehicle_rows if v["road"] is None),
        "vehicles": vehicle_rows,
        "roads": road_stats,
        "frames": frames,
        "fps": round(fps, 2),
        "duration": round(frames / fps, 2) if fps else 0,
        "resolution": f"{w}x{h}",
    }
