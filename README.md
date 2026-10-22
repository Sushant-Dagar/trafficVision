# 🚦 Traffic Intersection Vehicle Detection

Upload videos of traffic-signal intersections (3-, 4-, or 5-way). A pretrained
**YOLOv8** model detects every passing vehicle, **ByteTrack** tracks each one so it's
counted exactly once, and every vehicle is assigned to the **road it came from**. For
each road you get its own cropped video, counts by vehicle type, and a timeline of
when each vehicle arrived — plus an annotated overview of the whole junction.

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

1. **Upload** an intersection video (drag-and-drop, browse, or paste a URL). The app
   asks **how many roads meet at the junction**.
2. **Step 1 — detection.** The card shows *Detecting* while every vehicle is found and
   tracked. Videos process one at a time (CPU-friendly).
3. **Mark roads.** When detection finishes the card changes to *Mark roads*. Open it
   to see a still frame with one suggested outline per road, worked out from where
   vehicles actually drove (shown as faint lines). Adjust them:
   - drag a corner to move it, drag the small dots between corners to add one,
     double-click a corner to delete it, drag inside an outline to move it all,
   - **Redraw** a road by clicking around it, rename roads, or add/remove roads.
   Each outline should cover the stretch of road *leading into* the junction.
4. **Step 2 — counting.** Click *Confirm roads & count*. This reuses the saved
   tracking, so it's much faster than step 1.
5. **Open the finished video** to see, for each road:
   - a video cropped to that road (its vehicles boxed in the road's colour, a live counter),
   - counts by vehicle type,
   - a *vehicles over time* graph whose marker follows the video,
   - a list of every vehicle with its arrival time — click one to jump the video there.

   Below that: the full junction view, and a table of every vehicle with the road it
   came from and when it arrived.

Use **Edit roads** on a finished video to change the outlines and recount in seconds,
without re-running detection.

### How a vehicle is assigned to a road

A vehicle counts for the road whose outline its ground point (bottom-centre of its box)
is inside when it first appears. It must be on a road within ~1 second of first being
seen. This leaves out:
- parked vehicles off the road,
- vehicles that never enter any outline,
- tracker fragments picked up in the middle of the junction (the tracker briefly lost
  a vehicle and re-found it with a new id) — otherwise these would be counted twice.

- **parked vehicles** (on by default, can be switched off per video in *Edit roads*):
  a vehicle whose smoothed position never leaves its spot. The tracker loses a
  parked car whenever traffic passes in front of it and re-finds it under a new id,
  so all still ids at the same spot are treated as one car; if that spot is occupied
  for at least 2 s (and for at least 40% of that time), every one of those ids is
  parked. A track that *starts* at a parked car's spot is that car re-found too.
  A car that waits at a red light and then drives off isn't parked.
- **sub-second detections** (tracked for under 0.5 s): detector noise or a vehicle
  briefly re-found.

These show up as "not counted for a road", with the reason, and are boxed in grey in
the overview. The "Vehicles counted" tile only includes vehicles counted for a road.
The thresholds are in `backend/config.py` (`STATIONARY_*`, `COUNT_MIN_SECONDS`).

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
  main.py        FastAPI app: upload, list, detail, roads, video streaming + worker thread
  processor.py   pass 1: YOLO + ByteTrack, save tracks · pass 2: per-road videos + stats
  roads.py       suggest road outlines from vehicle paths, assign vehicles to roads
  config.py      paths, model settings, vehicle-class mapping
  jobs.py        file-backed job store (storage/jobs.json)
frontend/
  index.html     gallery + detail (single page)
  app.js         upload, polling, hash routing, rendering
  styles.css     styling
storage/
  uploads/       original uploaded videos
  outputs/       {job_id}.mp4 overview, {job_id}_road{n}.mp4 per road,
                 {job_id}.json stats, {job_id}_tracks.npy saved tracking,
                 {job_id}_frame.jpg + {job_id}_analysis.json for the road editor
```

---

## Notes

- **Counting scope:** each vehicle is counted once, on the road it came from. Vehicles
  already waiting on a road when the clip starts count for that road at 0:00.
- **Camera:** works best with a fixed camera (CCTV or a steady elevated view). A moving
  or panning camera makes the road outlines meaningless.
- **Accuracy:** the pretrained detector is strong on cars/buses/trucks/motorcycles;
  bicycles and heavily occluded vehicles in busy scenes are inherently harder.
- **Browser-playable video:** output is encoded as H.264 via `imageio-ffmpeg` (a
  bundled ffmpeg), so no system ffmpeg install is required.
