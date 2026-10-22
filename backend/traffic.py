"""Traffic-engineering numbers for a junction, from the saved vehicle tracks.

Everything here is computed after detection, from the tracks and the road
outlines, so it costs nothing extra in the slow pass:

* Turning movements - which road each counted vehicle left by, giving a
  from -> to matrix, with each movement labelled left / straight / right /
  U-turn.
* Queues - how many vehicles are stopped on each road, over time.
* Waiting time - how long each counted vehicle stood still on the road it
  came from before moving on.
* Flow and PCU - vehicles per hour and passenger car units (IRC:106-1990)
  per road.

Positions are the vehicle's ground point (bottom-centre of its box), the same
point used to assign roads.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np

from . import config, roads as roadgeo

TURN_ORDER = ["left", "straight", "right", "U-turn"]


# ---------------------------------------------------------------------------
# Where a vehicle is, frame by frame
# ---------------------------------------------------------------------------
def road_sequence(chunk: np.ndarray, polys: list[np.ndarray]) -> np.ndarray:
    """Road index the vehicle is on for each of its detections (-1 = none).

    Where outlines overlap, the road whose edge the point is furthest inside
    wins (the same rule used when assigning the road a vehicle came from).
    """
    import cv2

    pts = roadgeo.ground_points(chunk)
    inside = np.stack([roadgeo.points_in_polygon(pts, p) for p in polys], axis=1)
    seq = np.full(len(pts), -1, dtype=np.int64)
    hits = inside.sum(axis=1)
    single = hits == 1
    seq[single] = inside[single].argmax(axis=1)
    multi = np.flatnonzero(hits > 1)
    if len(multi):
        cv_polys = [p.astype(np.float32).reshape(-1, 1, 2) for p in polys]
        for k in multi:
            cands = np.flatnonzero(inside[k])
            pt = (float(pts[k, 0]), float(pts[k, 1]))
            seq[k] = int(max(cands, key=lambda i: cv2.pointPolygonTest(cv_polys[i], pt, True)))
    return seq


def stopped_mask(chunk: np.ndarray, fps: float) -> np.ndarray:
    """True for each detection where the vehicle is standing still.

    Speed is measured over a one-second window of a smoothed path, in
    vehicle widths per second, so it means the same thing near the camera
    and far away. Below STOP_SPEED (about walking pace for a car) = stopped.
    """
    pts = roadgeo.ground_points(chunk)
    frames = chunk[:, roadgeo.COL_FRAME]
    width = float(np.median(chunk[:, roadgeo.COL_X2] - chunk[:, roadgeo.COL_X1]))
    n = len(pts)
    if n < 2 or width <= 0:
        return np.zeros(n, dtype=bool)

    # Running median over ~0.5 s removes box jitter.
    win = max(1, int(round(fps * 0.25)))
    smooth = np.empty_like(pts)
    for i in range(n):
        smooth[i] = np.median(pts[max(0, i - win): i + win + 1], axis=0)

    half = max(1, int(round(fps * 0.5)))
    stopped = np.zeros(n, dtype=bool)
    for i in range(n):
        a, b = max(0, i - half), min(n - 1, i + half)
        dt = (frames[b] - frames[a]) / fps
        if dt <= 0:
            continue
        speed = np.linalg.norm(smooth[b] - smooth[a]) / dt / width
        stopped[i] = speed < config.STOP_SPEED
    return stopped


# ---------------------------------------------------------------------------
# Turning movements
# ---------------------------------------------------------------------------
def exit_road(seq: np.ndarray, origin: int) -> int | None:
    """Last road (other than the one it came from) the vehicle was seen on,
    if it spent at least EXIT_MIN_FRAMES detections there."""
    others = seq[(seq >= 0) & (seq != origin)]
    if not len(others):
        return None
    counts = Counter(others.tolist())
    for road in reversed(others.tolist()):
        if counts[road] >= config.EXIT_MIN_FRAMES:
            return int(road)
    return None


def _end_state(chunk: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Last position, velocity (px per frame, over the last ~0.5 s) and box width."""
    pts = roadgeo.ground_points(chunk)
    frames = chunk[:, roadgeo.COL_FRAME]
    width = float(np.median(chunk[-10:, roadgeo.COL_X2] - chunk[-10:, roadgeo.COL_X1]))
    k = int(np.searchsorted(frames, frames[-1] - fps * 0.5))
    k = min(k, len(pts) - 1)
    df = frames[-1] - frames[k]
    tail = np.median(pts[-3:], axis=0)
    vel = (tail - np.median(pts[k:k + 3], axis=0)) / df if df > 0 else np.zeros(2)
    return tail, vel, width


def find_continuations(
    tracks: dict[int, np.ndarray], counted: set[int], fps: float
) -> dict[int, int]:
    """{lost track id: track id that carries on as the same vehicle}.

    When the tracker loses a vehicle (usually in the middle of the junction,
    hidden behind another one) it may re-find it a moment later under a new
    id. A new track that starts within a second of another ending, close to
    where the old one was heading, is taken to be the same vehicle. Only
    uncounted tracks can be continuations, so nothing is double counted.
    """
    max_gap = fps * config.LINK_MAX_SECONDS
    starts = sorted(
        (int(c[0, roadgeo.COL_FRAME]), tid) for tid, c in tracks.items() if tid not in counted
    )
    ends = sorted(tracks.items(), key=lambda kv: kv[1][-1, roadgeo.COL_FRAME])
    taken: set[int] = set()
    links: dict[int, int] = {}
    for tid, chunk in ends:
        f_end = chunk[-1, roadgeo.COL_FRAME]
        pos, vel, width = _end_state(chunk, fps)
        best, best_d = None, None
        for f_start, cand in starts:
            if f_start <= f_end:
                continue
            if f_start - f_end > max_gap:
                break
            if cand in taken or cand == tid:
                continue
            first = roadgeo.ground_points(tracks[cand][:3]).mean(axis=0)
            predicted = pos + vel * (f_start - f_end)
            d = float(np.linalg.norm(first - predicted))
            if d <= max(config.LINK_MAX_WIDTHS * width, 30.0) and (best_d is None or d < best_d):
                best, best_d = cand, d
        if best is not None:
            links[tid] = best
            taken.add(best)
    return links


def heading_exit(
    chunk: np.ndarray, origin: int, polys: list[np.ndarray], fps: float
) -> int | None:
    """Road the vehicle was heading for when it disappeared from view.

    Used when a vehicle vanishes before reaching any road outline - behind a
    building, a tree or a bus. It must have been clearly moving, and one road
    must lie within HEADING_MAX_DEG of its final direction of travel.
    """
    pos, vel, width = _end_state(chunk, fps)
    speed = float(np.linalg.norm(vel)) * fps / max(width, 1.0)  # widths per second
    if speed < config.STOP_SPEED * 2:
        return None
    heading = math.degrees(math.atan2(vel[1], vel[0]))
    best, best_diff = None, None
    for i, p in enumerate(polys):
        if i == origin:
            continue
        # Direction to the nearest part of the road outline (not its centre,
        # which can be far off to one side).
        d = p - pos
        dists = np.hypot(d[:, 0], d[:, 1])
        target = p[dists.argmin()] if dists.min() > 0 else p.mean(axis=0)
        to = math.degrees(math.atan2(target[1] - pos[1], target[0] - pos[0]))
        diff = abs((to - heading + 180.0) % 360.0 - 180.0)
        if best_diff is None or diff < best_diff:
            best, best_diff = i, diff
    if best is not None and best_diff <= config.HEADING_MAX_DEG:
        return best
    return None


def road_angles(polys: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Angle of each road around the junction centre, in image space.

    Angles grow clockwise on screen (y points down). A camera looking down
    at the road shows it the way a map does, so this is also the clockwise
    order seen from above - perspective squashes angles but keeps the order.
    """
    cents = np.array([p.mean(axis=0) for p in polys])
    centre = cents.mean(axis=0)
    rel = cents - centre
    return np.degrees(np.arctan2(rel[:, 1], rel[:, 0])) % 360.0, centre


def turn_label(origin: int, exit_: int, angles: np.ndarray) -> str:
    """left / straight / right / U-turn, from the driver's point of view.

    Coming in from a road, the next road clockwise around the junction is on
    the driver's left (true whichever side of the road traffic drives on).
    For a 4-way junction the clockwise order alone decides it, which survives
    any camera angle. Otherwise the angle between the roads is used: roughly
    opposite = straight.
    """
    if origin == exit_:
        return "U-turn"
    n = len(angles)
    order = list(np.argsort(angles))
    k = (order.index(exit_) - order.index(origin)) % n
    if n == 2:
        return "straight"
    if n == 4:
        return {1: "left", 2: "straight", 3: "right"}[k]
    gap = (angles[exit_] - angles[origin]) % 360.0
    if abs(gap - 180.0) <= config.STRAIGHT_TOLERANCE_DEG:
        return "straight"
    return "left" if gap < 180.0 else "right"


# ---------------------------------------------------------------------------
# PCU (IRC:106-1990, urban roads)
# ---------------------------------------------------------------------------
def pcu_factors(type_counts: Counter) -> dict[str, float]:
    """PCU for each vehicle type, from IRC:106-1990.

    IRC gives two values per type: one when that type is 5% of traffic, one
    when it's 10% or more; in between the value is interpolated.
    """
    total = sum(type_counts.values()) or 1
    out = {}
    for vtype, (low, high) in config.PCU_IRC106.items():
        share = type_counts.get(vtype, 0) / total
        frac = min(max((share - 0.05) / 0.05, 0.0), 1.0)
        out[vtype] = round(low + (high - low) * frac, 3)
    return out


# ---------------------------------------------------------------------------
# Everything at once
# ---------------------------------------------------------------------------
def analyse(
    tracks: dict[int, np.ndarray],
    vehicles: dict[int, dict[str, Any]],
    assignment: dict[int, tuple[int, int]],
    polys: list[np.ndarray],
    fps: float,
    total_frames: int,
) -> dict[str, Any]:
    """Turning movements, queues, waits, flow and PCU.

    ``tracks`` are the vehicles that count for queues (parked cars and
    sub-second flickers already removed); ``assignment`` gives the road each
    counted vehicle came from.
    """
    n = len(polys)
    angles, _centre = road_angles(polys)
    duration_h = max(total_frames / fps, 1e-6) / 3600.0
    gap_max = max(2, int(round(fps * 0.25)))

    per_vehicle: dict[int, dict[str, Any]] = {}
    queue_diff = [np.zeros(total_frames + 2, dtype=np.int64) for _ in range(n)]
    links = find_continuations(tracks, set(assignment), fps)
    seqs: dict[int, np.ndarray] = {}

    for tid, chunk in tracks.items():
        seq = road_sequence(chunk, polys)
        seqs[tid] = seq
        stopped = stopped_mask(chunk, fps)
        frames = chunk[:, roadgeo.COL_FRAME].astype(np.int64)

        # Queue: frames where this vehicle is stopped on a road. Short gaps
        # in detection between two stopped sightings on the same road are
        # filled in.
        for k in range(len(frames)):
            road = seq[k]
            if road < 0 or not stopped[k]:
                continue
            f0 = frames[k]
            f1 = f0
            if (
                k + 1 < len(frames)
                and seq[k + 1] == road
                and stopped[k + 1]
                and frames[k + 1] - f0 <= gap_max + 1
            ):
                f1 = frames[k + 1] - 1
            queue_diff[road][f0] += 1
            queue_diff[road][f1 + 1] -= 1

    for tid, chunk in tracks.items():
        if tid not in assignment:
            continue
        seq = seqs[tid]
        stopped = stopped_mask(chunk, fps)
        frames = chunk[:, roadgeo.COL_FRAME].astype(np.int64)
        origin = assignment[tid][0]

        # Which road it left by: seen there; or seen there under the id the
        # tracker gave it after losing it; or the road it was heading for
        # when it disappeared (estimate).
        to, basis, chain = exit_road(seq, origin), "seen", [tid]
        last = tid
        while to is None and last in links and len(chain) <= 3:
            last = links[last]
            chain.append(last)
            to = exit_road(seqs[last], origin)
            basis = "linked"
        # Only for a vehicle that vanished mid-clip. One still in view when
        # the video ends simply hadn't left yet (e.g. waiting at a red light).
        # It must also have left its approach road: a vehicle lost while still
        # on it hasn't started its turn, so its direction says nothing yet.
        last_frame = tracks[last][-1, roadgeo.COL_FRAME]
        left_origin = seqs[last][-1] != origin
        if to is None and left_origin and last_frame < total_frames - fps * 0.5:
            to = heading_exit(tracks[last], origin, polys, fps)
            basis = "direction"
        if to is None:
            basis = None

        # Waiting: time standing still on the road it came from.
        wait_frames = 0
        for k in range(len(frames)):
            if seq[k] == origin and stopped[k]:
                nxt = frames[k + 1] if k + 1 < len(frames) else frames[k] + 1
                wait_frames += int(min(nxt - frames[k], gap_max + 1))
        on_origin = np.flatnonzero(seq == origin)
        censored = bool(
            len(on_origin)
            and (
                (stopped[on_origin[0]] and frames[on_origin[0]] <= 1 + gap_max)
                or (stopped[on_origin[-1]] and frames[on_origin[-1]] >= total_frames - gap_max)
            )
        )
        per_vehicle[tid] = {
            "to": to,
            "turn": turn_label(origin, to, angles) if to is not None else None,
            # seen | linked (same vehicle, new tracker id) | direction (estimate)
            "exit_basis": basis,
            "linked_ids": chain[1:],
            "wait": round(wait_frames / fps, 1),
            # Already waiting when the clip started, or still waiting when it
            # ended: the real wait was longer than what was seen.
            "wait_partial": censored,
        }

    # Queue per road over time, sampled ~4 times a second (max in each step).
    step = max(1, int(round(fps / 4)))
    queues = []
    for i in range(n):
        series = np.cumsum(queue_diff[i])[1: total_frames + 1]
        if not len(series):
            series = np.zeros(1, dtype=np.int64)
        pad = (-len(series)) % step
        sampled = np.pad(series, (0, pad), constant_values=0).reshape(-1, step).max(axis=1)
        queues.append(
            {
                "step": round(step / fps, 3),
                "values": sampled.astype(int).tolist(),
                "max": int(series.max()),
                "avg": round(float(series.mean()), 2),
            }
        )

    # PCU factors depend on the mix of the whole junction's counted traffic.
    type_counts = Counter(vehicles[tid]["type"] for tid in assignment)
    factors = pcu_factors(type_counts)

    roads_out = []
    matrix = [[0] * n for _ in range(n)]
    estimated = [[0] * n for _ in range(n)]
    not_seen_leaving = [0] * n
    for i in range(n):
        ids = [tid for tid, (road, _f) in assignment.items() if road == i]
        waits = [per_vehicle[t]["wait"] for t in ids]
        waited = [w for w in waits if w >= config.MIN_WAIT_SECONDS]
        turns: Counter = Counter()
        for t in ids:
            to = per_vehicle[t]["to"]
            if to is None:
                not_seen_leaving[i] += 1
            else:
                matrix[i][to] += 1
                turns[per_vehicle[t]["turn"]] += 1
                if per_vehicle[t]["exit_basis"] == "direction":
                    estimated[i][to] += 1
        pcu = sum(factors.get(vehicles[t]["type"], 1.0) for t in ids)
        roads_out.append(
            {
                "flow_vph": round(len(ids) / duration_h),
                "pcu": round(pcu, 1),
                "pcu_ph": round(pcu / duration_h),
                "turns": {k: turns[k] for k in TURN_ORDER if turns[k]},
                "not_seen_leaving": not_seen_leaving[i],
                "stopped_count": len(waited),
                "avg_wait": round(sum(waited) / len(waited), 1) if waited else 0.0,
                "max_wait": round(max(waits), 1) if waits else 0.0,
                "avg_wait_all": round(sum(waits) / len(waits), 1) if waits else 0.0,
                "queue": queues[i],
            }
        )

    labels = [
        [turn_label(i, j, angles) for j in range(n)] for i in range(n)
    ]
    return {
        "vehicles": per_vehicle,
        "roads": roads_out,
        "turning": {
            "matrix": matrix,
            # How many in each cell were estimated from direction of travel.
            "estimated": estimated,
            "labels": labels,
            "not_seen_leaving": not_seen_leaving,
        },
        "pcu_factors": factors,
        "duration_h": duration_h,
    }
