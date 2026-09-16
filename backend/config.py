"""Central configuration: paths, model settings, and the vehicle-class mapping.

Everything the rest of the app needs to know about *where* things live and *how*
vehicle classes map to the 2-wheeler / 4-wheeler grouping the user asked for lives
here, so it is easy to tweak in one place.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# backend/config.py  ->  project root is one level up from this file's folder.
BASE_DIR = Path(__file__).resolve().parent.parent

FRONTEND_DIR = BASE_DIR / "frontend"
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
JOBS_FILE = STORAGE_DIR / "jobs.json"

# Make sure the writable dirs exist even on a fresh checkout.
for _d in (UPLOAD_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Model / inference settings
# ---------------------------------------------------------------------------
# yolov8n (fastest) < yolov8s (default, balanced) < yolov8m (most accurate).
# Weights auto-download on first use. Override with the MODEL_NAME env var.
MODEL_NAME = os.environ.get("MODEL_NAME", "yolov8s.pt")

# Minimum detection confidence to keep a box.
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.35"))

# Inference image size (longest side). Smaller = faster, less accurate.
IMG_SIZE = int(os.environ.get("IMG_SIZE", "640"))

# Tracker config shipped with ultralytics. ByteTrack gives stable IDs so each
# vehicle is counted exactly once.
TRACKER = "bytetrack.yaml"

# A track must be seen in at least this many frames to be counted. Filters out
# spurious one-frame detections that would otherwise inflate the totals.
MIN_TRACK_FRAMES = int(os.environ.get("MIN_TRACK_FRAMES", "3"))

# Fallback FPS if the source video reports an invalid frame rate.
DEFAULT_FPS = 25.0

# Allowed upload extensions.
ALLOWED_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}

# ---------------------------------------------------------------------------
# Remote download settings (YouTube / Dropbox / direct URL)
# ---------------------------------------------------------------------------
# Only the first N seconds of a remote video are downloaded + analysed, so CPU
# processing stays reasonable. Configurable via the MAX_DOWNLOAD_SECONDS env var.
MAX_DOWNLOAD_SECONDS = int(os.environ.get("MAX_DOWNLOAD_SECONDS", "20"))

# Cap download resolution (keeps files small and inference fast).
MAX_DOWNLOAD_HEIGHT = int(os.environ.get("MAX_DOWNLOAD_HEIGHT", "720"))

# Some networks (corporate SSL-inspection proxies) present a self-signed root CA
# that Python's default bundle doesn't trust, breaking downloads with
# CERTIFICATE_VERIFY_FAILED. The downloader auto-detects this and falls back to
# unverified TLS (the payload is public video, so the risk is low). Set
# INSECURE_SSL=1 to skip verification from the start, or =0 to force it off.
INSECURE_SSL = os.environ.get("INSECURE_SSL", "") == "1"

# Path to the ffmpeg binary bundled with imageio-ffmpeg. Both the downloader
# (for trimming) and the processor (for encoding) reuse this — no system ffmpeg.
try:
    import imageio_ffmpeg

    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001 - resolved lazily elsewhere if this fails
    FFMPEG_EXE = None

# Videos auto-ingested on first server start so the gallery isn't empty.
#
# Two tiers, by how sure we can be that it's genuine fixed-camera CCTV footage
# of a traffic-light junction:
#
#   TIER 1 - Verified: a concrete video URL from a channel independently
#   confirmed as an official government traffic-camera feed (cross-checked
#   against the city/DOT's own .gov site, not just a text search match).
#
#   TIER 2 - Best-effort: "ytsearch1:<query>" entries. yt-dlp searches a pool
#   of YouTube results and only downloads the first one whose *title*
#   contains "cctv" (see downloader._find_cctv_video_url) — but a title match
#   is a text-level filter, not a manual watch-through, so treat these as
#   probably-right rather than confirmed.
#
# Set the env var SEED_VIDEOS_DISABLED=1 to turn seeding off entirely.
SEED_VIDEOS: list[dict] = [
    # --- Tier 1: verified official government CCTV feed ---
    {
        "url": "https://www.youtube.com/watch?v=OJBpDvcJw1E",
        "label": "Murfreesboro, TN — official city traffic-camera live feed (verified)",
    },

    # --- Tier 2: best-effort, title-filtered YouTube search ---
    {
        "url": "ytsearch1:4-way intersection cctv camera footage",
        "label": "4-way junction CCTV",
    },
    {
        "url": "ytsearch1:3-way T junction cctv camera footage",
        "label": "3-way T-junction CCTV",
    },
    {
        "url": "ytsearch1:5-way intersection cctv camera footage",
        "label": "5-way junction CCTV",
    },
    {
        "url": "ytsearch1:roundabout cctv camera footage",
        "label": "Roundabout CCTV",
    },
    {
        "url": "ytsearch1:traffic signal junction cctv footage",
        "label": "Signal junction CCTV",
    },
    {
        "url": "ytsearch1:busy road junction cctv camera footage",
        "label": "Busy junction CCTV",
    },
    {
        "url": "ytsearch1:rush hour junction cctv camera footage",
        "label": "Rush hour junction CCTV",
    },
    {
        "url": "ytsearch1:night traffic junction cctv camera footage",
        "label": "Night junction CCTV",
    },
    {
        "url": "ytsearch1:rainy day junction cctv camera footage",
        "label": "Rainy junction CCTV",
    },
    {
        "url": "ytsearch1:snow junction cctv camera footage",
        "label": "Snowy junction CCTV",
    },
    {
        "url": "ytsearch1:Delhi junction cctv camera footage",
        "label": "Delhi junction CCTV",
    },
    {
        "url": "ytsearch1:Mumbai junction cctv camera footage",
        "label": "Mumbai junction CCTV",
    },
    {
        "url": "ytsearch1:Bangalore junction cctv camera footage",
        "label": "Bangalore junction CCTV",
    },
    {
        "url": "ytsearch1:Chennai junction cctv camera footage",
        "label": "Chennai junction CCTV",
    },
    {
        "url": "ytsearch1:Hyderabad junction cctv camera footage",
        "label": "Hyderabad junction CCTV",
    },
    {
        "url": "ytsearch1:New York City intersection cctv camera footage",
        "label": "NYC junction CCTV",
    },
    {
        "url": "ytsearch1:Los Angeles intersection cctv camera footage",
        "label": "LA junction CCTV",
    },
    {
        "url": "ytsearch1:Chicago intersection cctv camera footage",
        "label": "Chicago junction CCTV",
    },
    {
        "url": "ytsearch1:London junction cctv camera footage",
        "label": "London junction CCTV",
    },
    {
        "url": "ytsearch1:Tokyo intersection cctv camera footage",
        "label": "Tokyo junction CCTV",
    },
    {
        "url": "ytsearch1:Bangkok intersection cctv camera footage",
        "label": "Bangkok junction CCTV",
    },
    {
        "url": "ytsearch1:Hanoi junction cctv camera footage",
        "label": "Hanoi junction CCTV",
    },
    {
        "url": "ytsearch1:Ho Chi Minh City junction cctv camera footage",
        "label": "Ho Chi Minh City junction CCTV",
    },
    {
        "url": "ytsearch1:Jakarta junction cctv camera footage",
        "label": "Jakarta junction CCTV",
    },
    {
        "url": "ytsearch1:Manila junction cctv camera footage",
        "label": "Manila junction CCTV",
    },
    {
        "url": "ytsearch1:Beijing intersection cctv camera footage",
        "label": "Beijing junction CCTV",
    },
    {
        "url": "ytsearch1:Shanghai junction cctv camera footage",
        "label": "Shanghai junction CCTV",
    },
    {
        "url": "ytsearch1:Seoul intersection cctv camera footage",
        "label": "Seoul junction CCTV",
    },
    {
        "url": "ytsearch1:Sao Paulo junction cctv camera footage",
        "label": "Sao Paulo junction CCTV",
    },
    {
        "url": "ytsearch1:Mexico City junction cctv camera footage",
        "label": "Mexico City junction CCTV",
    },
    {
        "url": "ytsearch1:Cairo junction cctv camera footage",
        "label": "Cairo junction CCTV",
    },
    {
        "url": "ytsearch1:Lagos junction cctv camera footage",
        "label": "Lagos junction CCTV",
    },
    {
        "url": "ytsearch1:Nairobi junction cctv camera footage",
        "label": "Nairobi junction CCTV",
    },
    {
        "url": "ytsearch1:Istanbul junction cctv camera footage",
        "label": "Istanbul junction CCTV",
    },
    {
        "url": "ytsearch1:Moscow junction cctv camera footage",
        "label": "Moscow junction CCTV",
    },
    {
        "url": "ytsearch1:Berlin junction cctv camera footage",
        "label": "Berlin junction CCTV",
    },
    {
        "url": "ytsearch1:Paris junction cctv camera footage",
        "label": "Paris junction CCTV",
    },
    {
        "url": "ytsearch1:Sydney junction cctv camera footage",
        "label": "Sydney junction CCTV",
    },
    {
        "url": "ytsearch1:Toronto junction cctv camera footage",
        "label": "Toronto junction CCTV",
    },
    {
        "url": "ytsearch1:Dubai junction cctv camera footage",
        "label": "Dubai junction CCTV",
    },
    {
        "url": "ytsearch1:small town junction cctv camera footage",
        "label": "Small-town junction CCTV",
    },
    {
        "url": "ytsearch1:rural road junction cctv camera footage",
        "label": "Rural junction CCTV",
    },
    {
        "url": "ytsearch1:highway on ramp junction cctv camera footage",
        "label": "Highway ramp junction CCTV",
    },
    {
        "url": "ytsearch1:pedestrian crossing junction cctv camera footage",
        "label": "Pedestrian crossing junction CCTV",
    },
    {
        "url": "ytsearch1:junction traffic jam cctv camera footage",
        "label": "Traffic jam junction CCTV",
    },
    {
        "url": "ytsearch1:fixed cctv camera road junction footage",
        "label": "Fixed CCTV road junction",
    },
    {
        "url": "ytsearch1:scooter motorcycle heavy junction cctv footage",
        "label": "Motorcycle-heavy junction CCTV",
    },
    {
        "url": "ytsearch1:four way stop junction cctv camera footage",
        "label": "4-way stop junction CCTV",
    },
    {
        "url": "ytsearch1:downtown junction cctv camera footage",
        "label": "Downtown junction CCTV",
    },
    {
        "url": "ytsearch1:live cctv traffic junction camera footage",
        "label": "Live CCTV junction feed",
    },
]

SEED_VIDEOS_DISABLED = os.environ.get("SEED_VIDEOS_DISABLED", "1") == "1"

# ---------------------------------------------------------------------------
# Vehicle class mapping (COCO ids)
# ---------------------------------------------------------------------------
# Each entry: coco_id -> {name, category (2-wheeler|4-wheeler), wheels, color(BGR)}
# `color` is the box color used when annotating frames (OpenCV uses BGR order).
VEHICLE_CLASSES: dict[int, dict] = {
    1: {"name": "bicycle",    "category": "2-wheeler", "wheels": 2, "color": (255, 191, 0)},
    3: {"name": "motorcycle", "category": "2-wheeler", "wheels": 2, "color": (0, 165, 255)},
    2: {"name": "car",        "category": "4-wheeler", "wheels": 4, "color": (0, 200, 0)},
    5: {"name": "bus",        "category": "4-wheeler", "wheels": 6, "color": (0, 0, 255)},
    7: {"name": "truck",      "category": "4-wheeler", "wheels": 6, "color": (200, 0, 200)},
}

# The class ids we ask YOLO to detect (everything in the mapping above).
TARGET_CLASS_IDS = sorted(VEHICLE_CLASSES.keys())
