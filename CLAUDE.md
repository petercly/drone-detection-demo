# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Flask web app serving a 4-panel "security center" dashboard for real-time drone detection. Four simulated camera feeds (local MP4s) are processed through Roboflow inference with centroid-based tracking and direction-of-travel computation, streamed as MJPEG to the browser.

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Install the custom Roboflow Workflow Block plugin (for inference server)
pip install -e drone_direction_plugin/

# Start Roboflow Inference Server (required, separate terminal)
docker run -it --rm -p 9001:9001 roboflow/roboflow-inference-server-gpu

# Run the app
python run.py
# Dashboard at http://localhost:5000
```

Requires `.env` file with Roboflow credentials (see `.env.example`). Requires four MP4 files in `videos/` (north.mp4, south.mp4, east.mp4, west.mp4).

## Architecture

**Request flow:** Browser `<img>` tags connect to `/video_feed/<name>` MJPEG endpoints. Each feed has a daemon thread (`CameraFeed` in `app/camera.py`) that loops: read frame → resize 640x480 → Roboflow inference → update `CentroidTracker` → draw OpenCV overlays → store frame behind a lock. The MJPEG generator in `app/routes.py` reads the latest frame at `DISPLAY_FPS` rate. A separate `/api/stats` endpoint is polled by `dashboard.js` every 1.5s for count/direction/alert/log data.

**Monitoring controls:** `/api/start_monitoring` and `/api/stop_monitoring` toggle `inference_enabled` on all feeds. Stop resets all tracking state. Button state syncs via polling.

**Alert debouncing:** Alerts require 3 consecutive frames with detections to trigger (600ms at 5fps) and 5 consecutive frames without to clear (1s). Prevents flickering at model confidence boundaries.

**Intercardinal warnings:** Two OR conditions — center→edge quadrant transition within 15s memory window, OR drone in edge quadrant with atan2 direction pointing toward that edge.

**Alert event log:** State transitions (detected/cleared) are appended to a global `alert_log` deque (maxlen=50), served via `/api/stats` and rendered as a scrolling event bar at the bottom of the dashboard.

**Two tracking implementations exist:**
- `app/tracker.py` — Client-side `CentroidTracker` used by `camera.py` (always active, handles stateful tracking across frames). Tracks centroid, direction, and confidence per object.
- `drone_direction_plugin/direction_tracker/v1.py` — `DirectionOfTravelBlockV1`, a Roboflow Workflow Block plugin with the same algorithm, packaged for the inference server. Used when `WORKFLOW_ID` is set in config.

**Inference modes:** If `WORKFLOW_ID` is set, `camera.py` calls `run_workflow()`; otherwise it calls `infer()` directly with the model ID. Either way, results are parsed into detection dicts and fed to the client-side tracker.

## Key Configuration (`app/config.py`)

- `PROCESS_FPS` (default 5): How often inference runs per feed. 4 feeds × 5 = 20 req/s to local server.
- `DISPLAY_FPS` (default 15): MJPEG serving rate (re-serves last annotated frame between inferences).
- `CONFIDENCE_THRESHOLD` (0.3): Minimum detection confidence.
- Tracker params: `MAX_DISTANCE_THRESHOLD` (100px), `MAX_FRAMES_MISSING` (5), `DIRECTION_WINDOW` (10 frames), `STATIONARY_THRESHOLD` (15px).

## Key Visual Features

- **Confidence scores** displayed on bounding box labels (e.g., `ID:3 NE 87%`)
- **Hovering drone indicator:** Stationary drones rendered in amber with concentric circles instead of direction arrows, labeled "HOVER"
- **Feed reconnection:** Processing threads auto-retry on video source failure with 5s backoff
- **Compass minimap:** Cardinal triangles (red = drone detected), intercardinal triangles (yellow = blind-spot warning)
- **Event log bar:** Scrolling timeline at bottom showing alert detected/cleared transitions

## No Test Suite

There are currently no automated tests. Verification is manual: run the app and check the dashboard.

## Additional Documentation

- `ARCHITECTURE.md` — Full architecture review with data flow diagrams, threading model, and scale analysis
- `PRD.md` — Product requirements document (gitignored, local only)
- `DEMO-DEVELOPMENT-PROCESS.md` — Development journal with retrospective (gitignored, local only)
