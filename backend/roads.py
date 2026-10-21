"""Road geometry for a junction video.

Two jobs live here:

1. ``suggest_roads`` - given every tracked vehicle path and the number of roads
   the user said meet at the junction, propose one outline (polygon) per road.
   It finds the junction centre from where vehicle paths cross, groups the
   points vehicles drive over by their angle around that centre, and turns
   each group into a padded outline. The user then fixes the outlines by hand
   in the browser, so this only has to be a good starting point.

2. ``assign_roads`` - given the confirmed outlines, decide which road each
   vehicle *came from*: the first road outline its ground point (bottom-centre
   of its box) falls inside. Using the bottom of the box rather than the
   centre keeps tall vehicles (buses, trucks) attached to the road surface
   they are actually on instead of whatever their roof overlaps.

Polygons are stored normalised to 0..1 (x / width, y / height) so they don't
depend on the display size of the frame in the browser.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

# One colour per road, shared with the frontend. Hex for the browser, BGR for OpenCV.
ROAD_COLORS_HEX = [
    "#f3b21f",  # yellow
    "#4a84b8",  # blue
    "#45ad6c",  # green
    "#e15646",  # red
    "#c878c8",  # magenta
    "#5ac8be",  # teal
    "#f08c3c",  # orange
    "#a0a0ff",  # lavender
]

MAX_ROADS = len(ROAD_COLORS_HEX)

# Row layout of the saved detections array (one row per vehicle per frame).
COL_FRAME, COL_TID, COL_CLS, COL_CONF, COL_X1, COL_Y1, COL_X2, COL_Y2 = range(8)


def road_color_hex(i: int) -> str:
    return ROAD_COLORS_HEX[i % len(ROAD_COLORS_HEX)]


def road_color_bgr(i: int) -> tuple[int, int, int]:
    h = road_color_hex(i).lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b, g, r)


# ---------------------------------------------------------------------------
# Track helpers
# ---------------------------------------------------------------------------
def group_tracks(rows: np.ndarray) -> dict[int, np.ndarray]:
    """Split the flat detections array into {track_id: rows sorted by frame}."""
    if rows is None or rows.size == 0:
        return {}
    order = np.lexsort((rows[:, COL_FRAME], rows[:, COL_TID]))
    rows = rows[order]
    tids = rows[:, COL_TID].astype(np.int64)
    splits = np.flatnonzero(np.diff(tids)) + 1
    return {int(chunk[0, COL_TID]): chunk for chunk in np.split(rows, splits)}


def ground_points(chunk: np.ndarray) -> np.ndarray:
    """Bottom-centre of each box: where the vehicle touches the road."""
    return np.stack(
        [(chunk[:, COL_X1] + chunk[:, COL_X2]) / 2.0, chunk[:, COL_Y2]], axis=1
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def points_in_polygon(pts: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Vectorised even-odd ray casting. pts (N,2), poly (M,2) -> bool (N,)."""
    if len(pts) == 0:
        return np.zeros(0, dtype=bool)
    x, y = pts[:, 0], pts[:, 1]
    inside = np.zeros(len(pts), dtype=bool)
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        crosses = (yi > y) != (yj > y)
        denom = (yj - yi) if (yj - yi) != 0 else 1e-12
        x_at = (xj - xi) * (y - yi) / denom + xi
        inside ^= crosses & (x < x_at)
        j = i
    return inside


def _angle_diff(a: np.ndarray | float, b: np.ndarray | float) -> np.ndarray:
    d = np.abs(np.asarray(a) - np.asarray(b)) % 360.0
    return np.minimum(d, 360.0 - d)


def _circular_mean(angles: np.ndarray) -> float:
    r = np.radians(angles)
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360.0)


def _side_label(angle_deg: float) -> str:
    """Image-space angle (y down) -> which side of the frame the road heads to."""
    up_angle = (-angle_deg) % 360.0  # flip so 90 = towards the top of the frame
    sides = ["right", "top-right", "top", "top-left", "left", "bottom-left", "bottom", "bottom-right"]
    return sides[int(round(up_angle / 45.0)) % 8]


def _junction_center(paths: list[np.ndarray], w: int, h: int) -> np.ndarray:
    """Least-squares point closest to every vehicle's straight-line path.

    Vehicles on different roads travel along lines that cross near the middle
    of the junction, so the point nearest to all of those lines is a good
    estimate of the centre. Falls back to the middle of all ground points (or
    the frame centre) when there aren't enough crossing directions.
    """
    A = np.zeros((2, 2))
    b = np.zeros(2)
    for p in paths:
        d = p[-1] - p[0]
        norm = np.linalg.norm(d)
        if norm < 1e-6:
            continue
        n = np.array([-d[1], d[0]]) / norm
        nn = np.outer(n, n)
        A += nn
        b += nn @ p[0]
    center = None
    if paths and np.linalg.cond(A) < 1e4:
        try:
            center = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            center = None
    if center is None:
        if paths:
            center = np.median(np.concatenate(paths), axis=0)
        else:
            center = np.array([w / 2.0, h / 2.0])
    center[0] = min(max(center[0], 0.08 * w), 0.92 * w)
    center[1] = min(max(center[1], 0.08 * h), 0.92 * h)
    return center


def _angle_peaks(angles: np.ndarray, n: int) -> list[float]:
    """Pick ``n`` dominant directions from a circular histogram of angles."""
    peaks: list[float] = []
    if len(angles):
        hist, _ = np.histogram(angles, bins=360, range=(0, 360))
        sigma = 8.0
        k = np.arange(-30, 31)
        kernel = np.exp(-0.5 * (k / sigma) ** 2)
        smooth = np.convolve(np.concatenate([hist[-30:], hist, hist[:30]]), kernel, mode="same")[30:-30]
        is_peak = (smooth >= np.roll(smooth, 1)) & (smooth >= np.roll(smooth, -1)) & (smooth > 0)
        candidates = sorted(np.flatnonzero(is_peak), key=lambda i: -smooth[i])
        min_sep = max(25.0, 360.0 / (2.5 * n))
        for c in candidates:
            if len(peaks) >= n:
                break
            if all(_angle_diff(c, p) >= min_sep for p in peaks):
                peaks.append(float(c))
    # Not enough distinct directions in the traffic: fill the widest gaps.
    while len(peaks) < n:
        if not peaks:
            peaks.append(270.0)  # start with the top of the frame
            continue
        s = sorted(peaks)
        gaps = [((s[(i + 1) % len(s)] - s[i]) % 360.0 or 360.0, s[i]) for i in range(len(s))]
        gap, start = max(gaps)
        peaks.append((start + gap / 2.0) % 360.0)
    return peaks


def _circular_kmeans(angles: np.ndarray, centers: list[float], iters: int = 12) -> tuple[np.ndarray, list[float]]:
    c = np.array(centers, dtype=float)
    labels = np.zeros(len(angles), dtype=int)
    for _ in range(iters):
        dists = np.stack([_angle_diff(angles, ci) for ci in c], axis=1)
        labels = dists.argmin(axis=1)
        for i in range(len(c)):
            members = angles[labels == i]
            if len(members):
                c[i] = _circular_mean(members)
    return labels, list(c)


def _ray_to_frame(center: np.ndarray, angle_deg: float, w: int, h: int) -> np.ndarray:
    """Where a ray from ``center`` at ``angle_deg`` leaves the frame."""
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    ts = []
    if dx > 1e-9:
        ts.append((w - 1 - center[0]) / dx)
    if dx < -1e-9:
        ts.append(-center[0] / dx)
    if dy > 1e-9:
        ts.append((h - 1 - center[1]) / dy)
    if dy < -1e-9:
        ts.append(-center[1] / dy)
    t = min(t for t in ts if t >= 0) if ts else 0.0
    return np.array([center[0] + t * dx, center[1] + t * dy])


def _wedge_polygon(center: np.ndarray, a1: float, a2: float, w: int, h: int) -> np.ndarray:
    """Sector from near the junction centre out to the frame edge, between two angles."""
    span = (a2 - a1) % 360.0 or 360.0
    r_in = 0.1 * min(w, h)
    steps = np.linspace(0, span, 7)
    inner = [center + r_in * np.array([math.cos(math.radians(a1 + s)), math.sin(math.radians(a1 + s))]) for s in steps]
    outer = [_ray_to_frame(center, a1 + s, w, h) for s in steps]
    return np.array(inner + outer[::-1])


def _outline_from_points(pts: np.ndarray, w: int, h: int) -> np.ndarray | None:
    """Padded, simplified outline around a cloud of ground points."""
    import cv2

    if len(pts) < 6:
        return None
    scale = 4  # work on a quarter-size mask: plenty precise, much faster
    mw, mh = max(8, w // scale), max(8, h // scale)
    hull = cv2.convexHull((pts / scale).astype(np.float32)).astype(np.int32)
    mask = np.zeros((mh, mw), dtype=np.uint8)
    cv2.fillPoly(mask, [hull], 255)
    pad = max(2, int(0.03 * math.hypot(mw, mh)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * pad + 1, 2 * pad + 1))
    mask = cv2.dilate(mask, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(contour, True)
    eps = 0.01 * peri
    approx = cv2.approxPolyDP(contour, eps, True)
    while len(approx) > 10:  # keep it to a handful of easy-to-drag corners
        eps *= 1.4
        approx = cv2.approxPolyDP(contour, eps, True)
    if len(approx) < 3:
        return None
    poly = approx.reshape(-1, 2).astype(float) * scale
    poly[:, 0] = np.clip(poly[:, 0], 0, w - 1)
    poly[:, 1] = np.clip(poly[:, 1], 0, h - 1)
    return poly


def _normalise(poly: np.ndarray, w: int, h: int) -> list[list[float]]:
    return [[round(float(x) / w, 4), round(float(y) / h, 4)] for x, y in poly]


def denormalise(poly: list[list[float]], w: int, h: int) -> np.ndarray:
    return np.array([[x * w, y * h] for x, y in poly], dtype=float)


# ---------------------------------------------------------------------------
# 1. Suggest road outlines
# ---------------------------------------------------------------------------
def suggest_roads(
    tracks: dict[int, np.ndarray], w: int, h: int, n_roads: int, min_frames: int
) -> list[dict[str, Any]]:
    n = max(1, min(int(n_roads or 4), MAX_ROADS))
    diag = math.hypot(w, h)

    paths = [ground_points(c) for c in tracks.values() if len(c) >= min_frames]
    moving = [p for p in paths if np.linalg.norm(p[-1] - p[0]) > 0.05 * diag]
    center = _junction_center(moving, w, h)

    samples = []
    for p in moving:
        step = max(1, len(p) // 30)
        samples.append(p[::step])
        samples.append(p[-1:])
    pts = np.concatenate(samples) if samples else np.zeros((0, 2))

    centers: list[float]
    labels = np.zeros(0, dtype=int)
    if len(pts) >= 4 * n:
        rel = pts - center
        dist = np.hypot(rel[:, 0], rel[:, 1])
        # Points right in the middle belong to every road at once; leave them out.
        keep = dist > max(np.percentile(dist, 25), 0.06 * min(w, h))
        pts = pts[keep]
        rel = rel[keep]
        angles = np.degrees(np.arctan2(rel[:, 1], rel[:, 0])) % 360.0
        labels, centers = _circular_kmeans(angles, _angle_peaks(angles, n))
    else:
        centers = _angle_peaks(np.zeros(0), n)

    # Order roads clockwise starting from the top of the frame.
    order = sorted(range(len(centers)), key=lambda i: (centers[i] - 270.0) % 360.0)
    sorted_centers = [centers[i] for i in order]

    roads = []
    for rank, idx in enumerate(order):
        c = centers[idx]
        poly = _outline_from_points(pts[labels == idx], w, h) if len(labels) else None
        if poly is None:
            # Fall back to a wedge halfway to each neighbouring road.
            prev_c = sorted_centers[(rank - 1) % len(sorted_centers)]
            next_c = sorted_centers[(rank + 1) % len(sorted_centers)]
            if len(sorted_centers) == 1:
                a1, a2 = c - 45.0, c + 45.0
            else:
                a1 = c - ((c - prev_c) % 360.0) / 2.0
                a2 = c + ((next_c - c) % 360.0) / 2.0
            poly = _wedge_polygon(center, a1 % 360.0, a2 % 360.0, w, h)
        roads.append(
            {
                "name": f"Road {rank + 1} ({_side_label(c)})",
                "polygon": _normalise(poly, w, h),
            }
        )
    return roads


def trajectories_for_display(
    tracks: dict[int, np.ndarray], w: int, h: int, min_frames: int, limit: int = 250
) -> list[list[list[float]]]:
    """Thinned vehicle paths (normalised) to draw under the road editor."""
    out = []
    long_first = sorted(
        (c for c in tracks.values() if len(c) >= min_frames), key=len, reverse=True
    )[:limit]
    for chunk in long_first:
        p = ground_points(chunk)
        step = max(1, len(p) // 16)
        thin = np.concatenate([p[::step], p[-1:]])
        out.append(_normalise(thin, w, h))
    return out


def validate_roads(raw: Any) -> list[dict[str, Any]]:
    """Clean user-submitted roads. Raises ValueError with a readable message."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("Mark at least one road.")
    if len(raw) > MAX_ROADS:
        raise ValueError(f"At most {MAX_ROADS} roads are supported.")
    cleaned = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise ValueError(f"Road {i + 1} is malformed.")
        poly = r.get("polygon")
        if not isinstance(poly, list) or len(poly) < 3:
            raise ValueError(f"Road {i + 1} needs at least 3 corners.")
        pts = []
        for p in poly:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                raise ValueError(f"Road {i + 1} has an invalid corner.")
            pts.append([round(min(max(x, 0.0), 1.0), 4), round(min(max(y, 0.0), 1.0), 4)])
        name = str(r.get("name") or f"Road {i + 1}").strip()[:40] or f"Road {i + 1}"
        cleaned.append({"name": name, "polygon": pts})
    return cleaned


# ---------------------------------------------------------------------------
# 2. Assign each vehicle to the road it came from
# ---------------------------------------------------------------------------
def assign_roads(
    tracks: dict[int, np.ndarray],
    roads: list[dict[str, Any]],
    w: int,
    h: int,
    max_delay_frames: int,
) -> dict[int, tuple[int, int]]:
    """{track_id: (road_index, frame_it_was_on_that_road)} for counted vehicles.

    "Came from" = the road outline the vehicle's ground point is inside when
    it first appears. A vehicle has to be on a road within
    ``max_delay_frames`` of first being seen to count at all. That rejects
    tracker fragments that start in the middle of the junction (the tracker
    briefly lost a vehicle and re-found it under a new id): without this they
    would drive into an exit road and be counted a second time as if they had
    come from there. Parked vehicles off the road never enter an outline and
    are left out too.

    If outlines overlap at that moment, the road whose edge the point is
    furthest inside wins.
    """
    import cv2

    polys = [denormalise(r["polygon"], w, h) for r in roads]
    cv_polys = [p.astype(np.float32).reshape(-1, 1, 2) for p in polys]
    result: dict[int, tuple[int, int]] = {}
    for tid, chunk in tracks.items():
        pts = ground_points(chunk)
        inside = np.stack([points_in_polygon(pts, p) for p in polys], axis=1)
        hit_rows = np.flatnonzero(inside.any(axis=1))
        if not len(hit_rows):
            continue
        k = int(hit_rows[0])
        if chunk[k, COL_FRAME] - chunk[0, COL_FRAME] > max_delay_frames:
            continue
        candidates = np.flatnonzero(inside[k])
        if len(candidates) == 1:
            road = int(candidates[0])
        else:
            pt = (float(pts[k, 0]), float(pts[k, 1]))
            road = int(max(candidates, key=lambda i: cv2.pointPolygonTest(cv_polys[i], pt, True)))
        result[tid] = (road, int(chunk[k, COL_FRAME]))
    return result
