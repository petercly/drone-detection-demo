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

**Request flow:** Browser `<img>` tags connect to `/video_feed/<name>` MJPEG endpoints. Each feed has a daemon thread (`CameraFeed` in `app/camera.py`) that loops: read frame → resize 640x480 → Roboflow inference → update `CentroidTracker` → draw OpenCV overlays → store frame behind a lock. The MJPEG generator in `app/routes.py` reads the latest frame at `DISPLAY_FPS` rate. A separate `/api/stats` endpoint is polled by `dashboard.js` every 1.5s for count/direction/alert data.

**Two tracking implementations exist:**
- `app/tracker.py` — Client-side `CentroidTracker` used by `camera.py` (always active, handles stateful tracking across frames)
- `drone_direction_plugin/direction_tracker/v1.py` — `DirectionOfTravelBlockV1`, a Roboflow Workflow Block plugin with the same algorithm, packaged for the inference server. Used when `WORKFLOW_ID` is set in config.

**Inference modes:** If `WORKFLOW_ID` is set, `camera.py` calls `run_workflow()`; otherwise it calls `infer()` directly with the model ID. Either way, results are parsed into detection dicts and fed to the client-side tracker.

## Key Configuration (`app/config.py`)

- `PROCESS_FPS` (default 5): How often inference runs per feed. 4 feeds × 5 = 20 req/s to local server.
- `DISPLAY_FPS` (default 15): MJPEG serving rate (re-serves last annotated frame between inferences).
- `CONFIDENCE_THRESHOLD` (0.3): Minimum detection confidence.
- Tracker params: `MAX_DISTANCE_THRESHOLD` (100px), `MAX_FRAMES_MISSING` (5), `DIRECTION_WINDOW` (10 frames), `STATIONARY_THRESHOLD` (15px).

## No Test Suite

There are currently no automated tests. Verification is manual: run the app and check the dashboard.
