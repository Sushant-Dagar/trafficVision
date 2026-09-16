# 🚦 Traffic Intersection Vehicle Detection

Upload videos of traffic-signal intersections (3-, 4-, or 5-way). A pretrained
**YOLOv8** model detects every passing vehicle, **ByteTrack** tracks each one so it's
counted exactly once, and you get an **annotated video** plus a full stats breakdown:
total vehicles, a 2-wheeler / 4-wheeler split, per-type counts, and a per-vehicle table
with type and number of wheels.

No GPU and no dataset/training required — it uses standard COCO vehicle classes
(car, truck, bus, motorcycle, bicycle) out of the box.

---

## Quick start (Windows)

Just double-click **`run.bat`**. It installs dependencies and starts the server.

Or manually:

```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser.

> **First run** downloads the YOLO weights (`yolov8s.pt`, ~22 MB) automatically. This
> needs an internet connection the first time only.

---

## How to use

1. **Upload** one or more intersection videos (drag-and-drop or browse).
2. Each video appears as a card that moves through **Queued → Processing → Done**.
   Videos process one at a time (CPU-friendly); the card shows a live progress bar.
3. **Click a "Done" card** to open its detail page:
   - the annotated video (bounding boxes + `type #id` labels),
   - summary tiles (total / 2-wheelers / 4-wheelers),
   - a breakdown-by-type table,
   - a per-vehicle table: **ID · type · category · wheels · average confidence**.

---

## Vehicle categories

| Detected type | Category   | Wheels shown |
|---------------|------------|--------------|
| bicycle       | 2-wheeler  | 2            |
| motorcycle    | 2-wheeler  | 2            |
| car           | 4-wheeler  | 4            |
| bus           | 4-wheeler  | 6            |
| truck         | 4-wheeler  | 6            |

Edit the mapping in [`backend/config.py`](backend/config.py) to change categories,
colors, or wheel counts.

---

## Performance & tuning

There's **no GPU** on this machine, so processing runs on CPU and takes a while
(expect on the order of minutes for a short clip, depending on resolution). To trade
accuracy for speed, set environment variables before launching:

| Variable          | Default       | Effect                                             |
|-------------------|---------------|----------------------------------------------------|
| `MODEL_NAME`      | `yolov8s.pt`  | `yolov8n.pt` = fastest, `yolov8m.pt` = most accurate |
| `IMG_SIZE`        | `640`         | Lower (e.g. `480`) = faster, less accurate         |
| `CONF_THRESHOLD`  | `0.35`        | Minimum detection confidence                       |
| `MIN_TRACK_FRAMES`| `3`           | Discard tracks seen in fewer frames (noise filter) |

Example (Git Bash):

```bash
MODEL_NAME=yolov8n.pt IMG_SIZE=480 python -m uvicorn backend.main:app --port 8000
```

---

## Project structure

```
backend/
  main.py        FastAPI app: upload, list, detail, video streaming + worker thread
  processor.py   YOLO track loop, annotate, H.264 encode, stats aggregation
  config.py      paths, model settings, vehicle-class mapping
  jobs.py        file-backed job store (storage/jobs.json)
frontend/
  index.html     gallery + detail (single page)
  app.js         upload, polling, hash routing, rendering
  styles.css     styling
storage/
  uploads/       original uploaded videos
  outputs/       annotated {job_id}.mp4 + {job_id}.json stats
```

---

## Notes

- **Counting scope:** totals are for the whole video (each vehicle counted once), not
  per intersection arm/direction.
- **Accuracy:** the pretrained detector is strong on cars/buses/trucks/motorcycles;
  bicycles and heavily occluded vehicles in busy scenes are inherently harder.
- **Browser-playable video:** output is encoded as H.264 via `imageio-ffmpeg` (a
  bundled ffmpeg), so no system ffmpeg install is required.
