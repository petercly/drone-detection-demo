# Drone Detection Security Center - PRD

## Context
Roboflow Field Engineering Tech Challenge: Build an end-to-end CV deployment demo. The use case is a security center monitoring 4 camera feeds (N/S/E/W) for drone intrusion detection, counting, direction-of-travel tracking, and blind-spot warnings. Must showcase data labeling, model training, **custom Workflow Block**, and deployment with actionable insights.

---

# Problem Alignment

## Problem & Opportunity
Security facilities lack real-time automated drone detection across multiple surveillance feeds. Manual monitoring is error-prone and doesn't scale. A centralized AI-powered dashboard can detect, count, and track drones across all perimeter cameras simultaneously, providing instant alerts and situational awareness — including warnings when drones move toward gaps between cameras.

## High Level Approach
A Python Flask web app serving a 4-panel "security center" dashboard. Each panel shows a simulated camera feed (local MP4 files) processed through a Roboflow Workflow via the hosted serverless API. A **custom Workflow Block** computes per-drone centroid positions and 3x3 quadrant mapping. A client-side centroid tracker computes direction-of-travel. Results are overlaid on video in real-time via MJPEG streaming.

## Goals
1. Live 4-feed dashboard with real-time drone detection overlays
2. Drone counting per feed and total
3. Direction-of-travel tracking (N/NE/E/SE/S/SW/W/NW) via client-side centroid tracker
4. Visual alerts when drones are detected (red compass, red panel glow)
5. Intercardinal blind-spot warnings (yellow compass) when drones move toward camera gaps
6. Custom Roboflow Workflow Block for per-frame quadrant mapping (challenge requirement)

## Non-goals
1. Actual RTSP camera integration (simulated with local MP4s)
2. Production-grade scalability or auth
3. Persistent data storage or historical analytics

---

# Solution Alignment

## Architecture

```
Browser (dashboard.html)
  ├── 4x <img src="/video_feed/{direction}">  (MJPEG streams)
  └── JS polls /api/stats every 1.5s

Flask App (run.py, port 8000)
  ├── 4x CameraFeed threads (one per video)
  │   ├── cv2.VideoCapture reads MP4 frames (640x480)
  │   ├── Calls Roboflow Workflow via InferenceHTTPClient
  │   ├── Client-side centroid tracker (atan2 direction + intercardinal warnings)
  │   ├── Draws bounding boxes, direction arrows, counts (OpenCV)
  │   └── Stores latest annotated frame (thread-safe)
  └── Routes: /, /video_feed/<name>, /api/stats

Roboflow Hosted API (serverless.roboflow.com)
  ├── Drone detection model (drone-detection-on-eo-ir v3, RF-DETR)
  └── Custom Workflow Block: Quadrant Mapper
      ├── Computes per-drone centroid from bounding box
      ├── Maps centroid to 3x3 grid quadrant (1-9)
      └── Computes swarm centroid and swarm quadrant
```

## Dual Tracking Architecture

The system uses two complementary approaches:

1. **Custom Workflow Block (Roboflow-side, per-frame)**: Computes spatial position — centroid coordinates and quadrant mapping on a 3x3 grid. Runs on Roboflow's hosted serverless API. Satisfies the challenge's "custom Workflow Block" requirement.

2. **Client-side centroid tracker (Flask-side, cross-frame)**: Maintains persistent object IDs across frames via nearest-neighbor distance matching. Computes direction-of-travel from centroid displacement vectors (atan2 → 8 compass directions). Derives intercardinal blind-spot warnings from horizontal movement patterns.

**Why this split**: The Workflow Block runs statelessly per-frame on a remote API — it cannot track objects across frames. The client-side tracker maintains frame-to-frame state but doesn't run on Roboflow. Together they provide both spatial (where is it?) and temporal (where is it going?) intelligence.

## Key Features

### 1. MJPEG Video Streaming (4 feeds)
- Each `CameraFeed` runs in a daemon thread, reading frames from local MP4
- Videos loop when they end for continuous demo playback
- Flask serves MJPEG streams at `/video_feed/<name>` via generator pattern
- Browser renders with simple `<img>` tags (no JS video handling needed)

### 2. Roboflow Inference Integration
- `InferenceHTTPClient` calls hosted Workflow at `serverless.roboflow.com`
- Configurable `PROCESS_FPS` (default 2) to balance latency vs throughput
- 4 concurrent feeds at 2 FPS = ~8 inference calls/sec via network
- `use_cache=True` for faster repeated requests

### 3. Custom Workflow Block: Quadrant Mapper
- **This satisfies the challenge's "custom Workflow Block" requirement**
- Built in Roboflow's visual Workflow editor with custom Python code
- Takes batch detection predictions (`object_detection_prediction`)
- Computes per-drone centroid from bounding box coordinates
- Maps each centroid to a 3x3 grid quadrant (1-9):
  ```
  1 (TL) | 2 (TC) | 3 (TR)
  4 (ML) | 5 (MC) | 6 (MR)
  7 (BL) | 8 (BC) | 9 (BR)
  ```
- Computes swarm centroid (average position of all drones)
- Outputs: quadrants list, centroids list, swarm_centroid, swarm_quadrant

### 4. Client-side Direction Tracker
- Centroid-based nearest-neighbor matching with persistent IDs (`app/tracker.py`)
- Sliding window of 10 frames for direction computation
- atan2 displacement vector → 8 compass directions
- Chosen over quadrant-sequence approach for accuracy (continuous angle vs. 9-cell grid)

### 5. Intercardinal Blind-Spot Warnings
- Cameras cover N/S/E/W but leave diagonal gaps (NE/SE/SW/NW)
- When a drone moves horizontally across a camera's field of view (>30px displacement), it's heading toward a blind spot
- Each camera maps horizontal movement to specific intercardinal directions:
  - NORTH cam: left→right = NE, right→left = NW
  - SOUTH cam: left→right = SW, right→left = SE
  - EAST cam: left→right = SE, right→left = NE
  - WEST cam: left→right = NW, right→left = SW
- Dashboard compass lights up yellow intercardinal triangles as warnings

### 6. Dashboard UI
- Dark theme (security center aesthetic)
- 2x2 CSS grid with camera labels (NORTH/SOUTH/EAST/WEST)
- SVG compass minimap at center with 8 directional triangles:
  - Cardinal (N/S/E/W): red when drones detected on that feed
  - Intercardinal (NE/SE/SW/NW): yellow when blind-spot warning active
- Per-feed: drone count badge, direction indicators with arrows, alert state
- Summary panel: total drone count, total unique, alert status, feeds online
- Red pulse animation on feeds with active detections

### 7. Overlays (drawn server-side via OpenCV)
- Green bounding boxes around detected drones
- Tracker ID labels
- Direction arrows (`cv2.arrowedLine`) from centroids
- "ALERT" badge when drones present
- Drone count in corner

## Key Logic
- **Thread safety**: Each CameraFeed uses `threading.Lock` for frame access
- **FPS decoupling**: Inference runs at `PROCESS_FPS` (2), display runs at `DISPLAY_FPS` (10)
- **Tracking algorithm**: Centroid distance matching with `max_distance_threshold` (100px) and `max_frames_missing` (5 frames) before deregistration
- **Direction calculation**: Vector from oldest to newest centroid in sliding window (last 10 frames), converted to compass direction via atan2. "Stationary" if displacement < 15px threshold.
- **Lazy initialization**: Feeds start on first HTTP request via `@app.before_request`, not at import time. Prevents GIL starvation during Flask socket binding.

---

# Development & Launch Planning

## Dependencies (requirements.txt)
```
flask>=3.0
opencv-python-headless>=4.8
inference-sdk>=0.30
python-dotenv>=1.0
numpy<2
scipy>=1.11
```

## Risks & Mitigations
- **Workflow block state** (materialized): Hosted serverless API creates fresh block instances per call — no state persistence. **Mitigated**: Client-side tracker handles cross-frame tracking; Workflow Block handles single-frame enrichment only.
- **Inference throughput**: 4 feeds at 2 FPS = 8 req/s via network. API latency is 300-800ms. **Mitigated**: `PROCESS_FPS=2` keeps within budget; `use_cache=True` helps.
- **Browser connection limits**: 4 MJPEG streams + API polling = 5 connections (within browser's 6-per-host limit).
- **CPU constraints**: Intel i7-7660U (2-core, 2017) cannot run RF-DETR locally. **Mitigated**: All inference offloaded to Roboflow hosted API.
