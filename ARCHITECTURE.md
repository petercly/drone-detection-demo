# Drone Detection Security Center — Architecture Document

---

## 1. System Overview

A single-process Flask application serving a 4-panel "security operations center" dashboard. Four threads each loop a local MP4 file through a Roboflow inference server, track detected objects across frames using centroid distance matching, compute direction of travel from position history, and serve annotated frames as MJPEG streams to a browser. A polling loop fetches aggregate stats every 1.5 seconds to update the floating center compass overlay and per-feed scrolling activity logs.

**Layout:** 2×2 CSS grid with feeds arranged geographically (N=top-left, E=top-right, W=bottom-left, S=bottom-right). The compass and controls float as an absolutely-positioned overlay at the grid center, decoupled from feed sizing. Per-feed activity logs render as scrolling strips in the pillarbox areas beside each 4:3 video.

### System Diagram

```mermaid
graph TB
    subgraph Browser["Browser (dashboard.html)"]
        IMG1["&lt;img&gt; /video_feed/north"]
        IMG2["&lt;img&gt; /video_feed/east"]
        IMG3["&lt;img&gt; /video_feed/west"]
        IMG4["&lt;img&gt; /video_feed/south"]
        JS["dashboard.js<br/>polls /api/stats every 1.5s"]
        COMPASS["Floating Center Overlay<br/>compass + stats + controls"]
        LOGS["Per-feed Log Strips<br/>in pillarbox areas"]
    end

    subgraph Flask["Flask App (single process)"]
        ROUTES["routes.py<br/>/video_feed, /api/stats<br/>/api/start_monitoring<br/>/api/stop_monitoring"]

        subgraph Threads["4× Daemon Threads"]
            CF_N["CameraFeed: north"]
            CF_E["CameraFeed: east"]
            CF_W["CameraFeed: west"]
            CF_S["CameraFeed: south"]
        end

        subgraph Processing["Per-thread Processing Loop"]
            READ["cv2.VideoCapture.read()"]
            RESIZE["Resize 640×480"]
            INFER["Roboflow Inference<br/>(if monitoring enabled)"]
            TRACK["CentroidTracker.update()<br/>greedy centroid matching<br/>atan2 direction computation"]
            WARN["Intercardinal Warnings<br/>quadrant memory OR<br/>atan2 + edge position"]
            DRAW["OpenCV Overlays<br/>bboxes, arrows, labels,<br/>confidence, hover indicator"]
            STORE["Store annotated frame<br/>(thread-locked)"]
            LOG["Feed Activity Log<br/>deque(maxlen=100)"]
        end
    end

    subgraph Inference["Roboflow Inference Server"]
        MODEL["RF-DETR Drone Detection<br/>drone-detection-on-eo-ir"]
        WFB["Custom Workflow Block<br/>(optional: quadrant mapping)"]
    end

    subgraph Video["Video Sources"]
        MP4_N["north.mp4"]
        MP4_E["east.mp4"]
        MP4_W["west.mp4"]
        MP4_S["south.mp4"]
    end

    MP4_N --> CF_N
    MP4_E --> CF_E
    MP4_W --> CF_W
    MP4_S --> CF_S

    CF_N & CF_E & CF_W & CF_S --> READ
    READ --> RESIZE --> INFER
    INFER -->|HTTP POST| MODEL
    MODEL -->|predictions| TRACK
    TRACK --> WARN --> DRAW --> STORE
    TRACK --> LOG

    STORE -->|MJPEG stream| ROUTES
    LOG -->|/api/stats JSON| ROUTES

    ROUTES -->|multipart/x-mixed-replace| IMG1 & IMG2 & IMG3 & IMG4
    ROUTES -->|JSON| JS
    JS --> COMPASS & LOGS
```

**What it demonstrates:** The pipeline from detection → tracking → situational awareness display. The intercardinal compass overlay is the key demo artifact — it answers "which direction is the drone heading relative to the perimeter," which is the operational question a security operator actually cares about.

**What it explicitly is not:** Production-grade. No auth, no persistence, no redundancy, no real RTSP input, no multi-operator support, no alert history, no audit log, no graceful degradation when the inference server is down.

---

## 2. Component Breakdown

### `app/config.py`
Single source of truth for tunable constants. `PROCESS_FPS` and `DISPLAY_FPS` are deliberately decoupled — inference runs at `PROCESS_FPS` while display runs at `DISPLAY_FPS`, meaning the MJPEG generator re-serves the last annotated frame between inference updates. This prevents choppy video without burning GPU cycles.

Threshold scrutiny:
- `MAX_DISTANCE_THRESHOLD = 100px` — on a 640x480 frame at 5fps, a drone moving at 200px/s travels 40px between frames. 100px gives 2.5x headroom.
- `DIRECTION_WINDOW = 10 frames` — at 5fps that's a 2-second history window. Long enough to smooth noise, short enough to capture direction changes.
- `STATIONARY_THRESHOLD = 15px` — total displacement across the window, not per-frame. A drone must move at least 7.5px/s sustained to register as moving. Slow-moving or hovering drones will be classified as stationary.

### `app/camera.py` — `CameraFeed`

The core processing unit. Each instance owns one video file, one `CentroidTracker`, one `InferenceHTTPClient`, and one annotated frame behind a threading lock.

**Per-loop execution order:**
1. Read next frame from `VideoCapture` (loops on EOF, resets tracker on loop)
2. Resize to 640x480 (normalization so all downstream math assumes this resolution)
3. Run inference (skipped if `inference_enabled=False` or API key missing)
4. Pass detection list to `CentroidTracker.update()`
5. Derive intercardinal warnings from quadrant history + atan2 direction
6. Draw OpenCV overlays onto a copy of the frame
7. Store annotated frame under lock

**The `inference_enabled` flag**: Feeds start processing video immediately but skip inference until the operator clicks "Start Monitoring." This means MJPEG streams show live (unannotated) video from load — the cognitive load of "it's already running, just not detecting" is lower than "nothing works until you click start."

**The `INTERCARDINAL_MAP`** encodes the physical layout: all cameras face **outward** from the protected area, providing all-round perimeter coverage. The operator at the center looks at feeds showing what's outside the perimeter in each cardinal direction. The "north" camera faces north, so screen-right corresponds to East — a drone at the right edge of the north feed is moving toward the NE gap between the N and E cameras. The mapping assumes standard (non-mirrored) camera orientation where screen-right = clockwise from the camera's facing direction.

```python
INTERCARDINAL_MAP = {
    "north": {"right": "NE", "left": "NW"},
    "south": {"right": "SW", "left": "SE"},
    "east":  {"right": "SE", "left": "NE"},
    "west":  {"right": "NW", "left": "SW"},
}
```

### `app/tracker.py` — `CentroidTracker`

A greedy assignment tracker using `scipy.spatial.distance.cdist` to build a full distance matrix, then matching greedily by smallest distance first. O(n²) in detections per frame — for n < 20 drones this is fast and the difference from optimal (Hungarian) assignment is negligible.

Key lifecycle: `_register()` assigns a new ID and starts a history deque. `_deregister()` removes after `MAX_FRAMES_MISSING` consecutive misses. `total_unique` is monotonically increasing — it tracks total IDs ever assigned, not current count.

Direction is computed via `vector_to_compass()` which uses `atan2(-dy, dx)` (negating dy to convert from image-y-down to math-y-up convention), then shifts via `(90 - angle_deg) / 45` to convert from atan2 convention (0°=East) to compass convention (0°=North), binning into 8 directions.

### `app/routes.py`

Thin routing layer with three notable decisions:

1. **MJPEG via `yield`:** The generator runs in its own thread per client connection. It sleeps `1/DISPLAY_FPS` between yields — a busy-wait approximation that drifts under load but is fine for a demo.

2. **`/api/stats` is poll-based:** The frontend polls every 1.5 seconds, creating a fixed latency floor. MJPEG annotation appears immediately (at inference FPS), but compass/counter updates lag by up to 1.5s.

3. **Start/Stop monitoring:** `/api/start_monitoring` enables inference on all feeds; `/api/stop_monitoring` disables inference and resets all tracking state.

### `dashboard.js`

Polling loop with direct DOM manipulation. The compass minimap maps 4 cardinal triangles to `drone_count > 0` (red) and 4 intercardinal triangles to `intercardinal_warnings` (yellow). Button state syncs from `monitoring_active` on every poll, so a second browser tab or page refresh reflects current state.

### `drone_direction_plugin/direction_tracker/v1.py`

A stateless per-frame Roboflow Workflow Block. Computes quadrant assignments and swarm centroid server-side. Key difference from `tracker.py`: it has no memory between frames. It cannot compute direction of travel. When `WORKFLOW_ID` is set, quadrant data comes from the server and direction data from the client — a split architecture that's not immediately obvious from reading either file in isolation.

---

## 3. Key Calculations & Transformations

### 3.1 Centroid Tracking — The Assignment Problem

On each frame, the tracker has existing object positions and new detection centroids. It must decide which detection corresponds to which object.

**Distance matrix:** `D[i][j] = euclidean_distance(existing[i], new[j])`

**Greedy matching:**
1. For each existing object (row), find minimum distance column
2. Sort rows by their minimum distance (closest match first)
3. Walk sorted rows: if neither row nor column is used, and distance < `MAX_DISTANCE_THRESHOLD`, match them
4. Unmatched existing objects increment `disappeared` counter
5. Unmatched new detections spawn new IDs

### 3.2 Direction of Travel — atan2 Computation

```
dx = new_x - old_x   (newest vs oldest in DIRECTION_WINDOW)
dy = new_y - old_y   (positive = downward in image space)

angle = atan2(-dy, dx)   // negate dy: image-y-down → math-y-up
angle_deg = degrees(angle)

// Rotate from atan2 convention (0°=East) to compass (0°=North)
index = round((90 - angle_deg) / 45) % 8
direction = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][index]
```

The `90 - angle_deg` shift converts atan2's 0°=East to compass 0°=North. Moving screen-up → angle=90° → index=0 → "N". Moving screen-right → angle=0° → index=2 → "E". Correct.

### 3.3 Quadrant Mapping — 3x3 Grid

```python
col = min(int(x / (640 / 3)), 2)   # 0, 1, 2
row = min(int(y / (480 / 3)), 2)   # 0, 1, 2
quadrant = row * 3 + col + 1       # 1-9, row-major
```

```
1 (TL) | 2 (TC) | 3 (TR)
4 (ML) | 5 (MC) | 6 (MR)
7 (BL) | 8 (BC) | 9 (BR)
```

Left edge = {1, 4, 7}, Center column = {2, 5, 8}, Right edge = {3, 6, 9}.

### 3.4 Intercardinal Warning Logic — OR Trigger

Two independent conditions, either sufficient:

**Condition 1 — Trajectory inference:**
- Was any center-column quadrant (2, 5, 8) occupied within the last 15 seconds?
- AND is any drone currently in a right-edge or left-edge quadrant?
- → Fire intercardinal warning for that edge direction

This encodes: "it was in the middle, now it's at the edge, it must be heading toward the corner." The 15-second window is generous — handles slow-movers and inference gaps.

**Condition 2 — Direct atan2 confirmation:**
- Is any drone currently in an edge quadrant?
- AND does its atan2 direction point toward that edge? (e.g., right-edge + direction ∈ {E, NE, SE})
- → Fire warning regardless of history

The OR logic increases sensitivity (fewer missed warnings) at the cost of more false positives. For a security application, this is the correct tradeoff.

### 3.5 MJPEG Encoding Pipeline

```
numpy.ndarray (BGR, uint8, 640x480x3)
  → cv2.imencode(".jpg", quality=80)
  → ~25-50KB JPEG bytes
  → MIME multipart framing
  → HTTP chunked response via generator yield
  → Browser <img> tag render
```

At `DISPLAY_FPS=15` × 4 feeds = 1.5-3MB/s total. On localhost this is invisible; over a real network it becomes the primary bottleneck.

---

## 4. Data Flow Diagram

```
VIDEO FILE (north.mp4)
        │
        │ cv2.VideoCapture.read()  [CameraFeed thread, ~5fps]
        ▼
RAW FRAME (native resolution)
        │
        │ cv2.resize(640, 480)
        ▼
NORMALIZED FRAME (640x480 BGR)
        │
        ├─── [if inference_enabled] ───┐
        │                              │
        │                              │ InferenceHTTPClient
        │                              │ HTTP POST to inference server
        │                              ▼
        │                    ROBOFLOW INFERENCE SERVER
        │                              │ Model forward pass
        │                              │ → predictions[]
        │                              │ {x, y, width, height, confidence, class}
        │                              │
        │                    _parse_*_result()
        │                              ▼
        │                    DETECTION LIST
        │                    [{x, y, w, h, confidence, class}, ...]
        │                              │
        │                    CentroidTracker.update()
        │                    - distance matrix (cdist)
        │                    - greedy assignment
        │                    - update history deques
        │                    - compute atan2 direction per object
        │                              ▼
        │                    TRACKED OBJECTS
        │                    {id: {centroid, direction}, ...}
        │                              │
        ├◄─────────────────────────────┘
        │
        │ _update_intercardinal_warnings()
        │ - quadrant history (15s window)
        │ - center→edge OR atan2+edge check
        │
        │ _draw_overlays()
        │ - bounding boxes, ID labels, direction arrows
        │ - feed name, drone count, alert badge
        ▼
ANNOTATED FRAME (640x480 BGR)
        │
        │ threading.Lock → self.frame = annotated
        ▼
FRAME BUFFER (per-feed, single frame)

─── MJPEG GENERATOR (per browser connection, ~15fps) ───

        │ feeds[name].get_frame()
        ▼
LATEST ANNOTATED FRAME
        │
        │ cv2.imencode(".jpg", quality=80)
        ▼
HTTP CHUNKED RESPONSE → Browser <img> → RENDERED PIXEL

─── STATS POLLING (browser, every 1500ms) ───

fetch('/api/stats') → JSON → DOM update (compass, counters, alerts)
```

---

## 5. Threading & Concurrency Model

### Thread inventory
- 1 main Flask thread (request handling)
- 4 daemon `CameraFeed` processing threads (one per feed)
- N MJPEG generator threads (one per active browser `<img>` connection)

### The lock
Each `CameraFeed` has one `threading.Lock` protecting `self.frame`. The processing thread acquires it to write; the MJPEG generator acquires it to read. `get_stats()` reads `drone_count`, `directions`, `alert`, etc. **without** acquiring the lock — these are individually atomic (GIL guarantees single `STORE_ATTR` bytecode), but reading multiple attributes is not atomic as a group. In practice this is harmless for a demo (you might get count=1 with empty directions for one poll cycle).

### GIL interaction
The CPU-bound portions (OpenCV operations, numpy cdist) release the GIL because they're C extensions. The inference HTTP call releases during network I/O. The 4 processing threads do genuinely run in parallel for the expensive operations, which is why this architecture works in CPython.

### Sleep yield
```python
sleep_time = max(frame_interval - elapsed, 0.05)
```
The 50ms minimum floor ensures cooperative yielding even when inference is fast. Without this, a fast loop would starve the MJPEG generator threads.

### What breaks under load
4 MJPEG connections × 15fps = 60 JPEG encodes/second. A second browser doubles this. At some point encode throughput saturates a CPU core and streams get choppy — but this manifests as lower frame rate, not errors.

---

## 6. Demo Improvements

### Implemented

- **6.1 Alert debouncing** — Requires 3 consecutive frames with detections to trigger (600ms at 5fps) and 5 consecutive frames without to clear (1s). Prevents flickering at model confidence boundaries.
- **6.2 Confidence display on overlays** — Confidence scores shown on bounding box labels (e.g., `ID:0 HOVER 91%`). Surfaces when the model is struggling.
- **6.3 Per-feed activity logs** — Scrolling log strips in the pillarbox areas beside each video feed. Each feed maintains a `deque(maxlen=100)` of timestamped events. Logs update every 1.5s during active alerts, persist across stop/start cycles, auto-scroll to newest entry. Positioned at left edge for N/W feeds, right edge for E/S feeds.
- **6.5 Feed reconnection on error** — Outer retry loop in `_process_loop` handles video open failures and end-of-file with 5s backoff. Tracker resets on reconnect.
- **6.7 Hovering drone indicator** — Stationary drones rendered in amber with concentric circles instead of direction arrows, labeled "HOVER".

### Remaining

- **6.4 Swarm detection visual** — The `swarm_quadrant` from the workflow plugin is computed but never rendered. A distinct visual indicator when multiple drones share a quadrant would be compelling.
- **6.6 Inference server health check** — If the Docker container isn't running, inference calls fail silently. A startup check with a dashboard warning ("Inference server unreachable") would save demo debugging time.

---

## 7. Scale Architecture

### 7.1 Design Philosophy

Even state-of-the-art drone detection models exhibit high missed detection rates and significant false alarm rates under real-world conditions. The architectural implication: **this system is a human attention-direction tool, not an autonomous detector.** The operator's judgment is the final detection layer. Architecture must preserve operator trust (avoid alert fatigue) while minimizing missed detections that erode credibility.

A system that cries wolf on every cloud has failed, regardless of how clean the code is. A system with 1-second latency that correctly identifies 70% of real drones is better than a 100ms system that fires 5 false alarms per minute.

### 7.2 Input Layer — Real RTSP Feeds

**Current:** `cv2.VideoCapture("videos/north.mp4")` looping local files with reconnection on failure (5s backoff).

**Production requires:**
- Network jitter buffering (`cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)` for latency vs. stability tradeoff)
- Reconnection with exponential backoff
- Frame timestamp preservation (RTSP PTS) — need to know when a frame was captured, not processed
- Camera metadata (PTZ position, zoom, calibration) for real-world coordinate projection

**Small object challenge:** At deployment standoff distances, drones can subtend <32x32 pixels — less than 0.4% of a 640x480 frame. Detection models show performance cliffs below this size. Requires either: (a) higher-resolution input with tiled inference (overlapping 640x640 patches, merged NMS), or (b) model fine-tuned for deployment environment. Neither is a software fix — it's a data/model problem the architecture must accommodate.

### 7.3 Inference Layer — The Throughput Bottleneck

**Current:** 4 feeds × 5fps = 20 inference req/s, synchronous HTTP calls blocking processing threads.

**At scale:** A single GPU handles ~30-100 fps for YOLO. At 16 feeds × 5fps = 80fps — potentially maxing a mid-tier GPU. At 32 feeds, need:
- Async inference with queues: threads push frames to Redis/RabbitMQ, GPU workers pull and return results
- Batching across feeds (4 frames in one API call) roughly halves latency and doubles throughput

**The synchronous bottleneck:** At 5fps with 200ms inference, the thread is blocked 100% of the time on inference. The `max(..., 0.05)` sleep prevents spin but effective rate is capped by inference latency. At scale, this must become async.

### 7.4 Tracking Layer — Stateful, Single-Process

**Current:** Greedy centroid tracker with hovering drone detection (amber "HOVER" indicator with concentric circles for stationary drones).

**Production gaps:**

1. **No persistence:** Process restart loses all tracker state (IDs, histories, unique counts). During an active incident, losing tracking continuity is operationally dangerous.

2. **No cross-camera correlation:** Each camera has independent ID counters. Object ID 3 on north and ID 3 on east are different objects. A drone exiting one camera's FOV and entering another's gets a new ID. Production needs persistent cross-camera drone IDs via re-identification embeddings.

3. **Association quality:** Greedy centroid distance fails when drones pass close together (identity switch) or are occluded beyond `MAX_FRAMES_MISSING`. SORT/DeepSORT (Kalman filter + IoU matching + re-ID embeddings) provides dramatically better track continuity and enables cross-camera tracking.

### 7.5 Alert Architecture — Managing Alert Fatigue

**Current:** Alert debouncing implemented (3 frames on, 5 frames off). Per-feed activity logs with timestamped detection/cleared events and drone direction info. Confidence scores displayed on all overlays.

**Production requirements (not yet implemented):**

- **Confidence aggregation:** Track rolling mean confidence; alert only when mean exceeds a higher threshold (e.g., 0.6) rather than the detection threshold (0.3).
- **Alert severity levels:** A drone heading toward the protected area is categorically more urgent than one heading away. Production needs threat scoring: distance to protected area + heading + speed + confidence + simultaneous detections.
- **Persistent audit log:** Current per-feed logs are in-memory deques. Production needs persistent storage with full detection metadata (drone ID, confidence, position, direction) for post-incident analysis.
- **Operator acknowledgment:** Unacknowledged alerts have unknown state. Production SOC tooling requires ack/dismiss workflows.

### 7.6 Multi-Operator & Multi-Site

**Current:** Single process, single server, no auth, no sessions.

**Production:**

- **Auth/RBAC:** Who can view feeds, start/stop monitoring, dismiss alerts?
- **WebSocket push:** Replace polling with events-on-change. At 10 operators polling 1.5s = 400 requests/minute for no reason.
- **WebRTC:** MJPEG has no inter-frame compression. Over WAN, WebRTC with H.264/VP9 gives 10-50x better bandwidth efficiency with congestion adaptation.
- **Horizontal scaling:** Requires extracting tracker state to external store (Redis) and making feed workers stateless.

### 7.7 Environmental Robustness

A single model and single confidence threshold does not work reliably across changing conditions.

**The performance cliff problem:** A model calibrated on daytime footage with 0.3 threshold may give 85% detection rate. At dusk, the same threshold may give 30% detection or 5x more false positives. The current architecture has no mechanism to detect or adapt.

**Architectural responses:**
- **Condition detection:** Frame brightness/contrast metrics classify "day / dusk / night / fog" → condition-adaptive confidence thresholds
- **Model ensemble:** Run two models in parallel (general + condition-specific), alert if either detects. Trades false positive rate for missed detection rate — correct for security, but requires the debouncing layer to prevent fatigue.
- **Performance monitoring:** Log per-detection confidence distributions. If rolling mean drops significantly, flag "model degradation" to operators.

### 7.8 Detection Difficulty Estimation

Predicting detection difficulty from drone size, background clutter, and motion blur enables proactive operator communication. An operational system should compute a difficulty score per frame/quadrant and display it: "Detection confidence in NW quadrant currently reduced due to background complexity." This sets operator expectations and reduces post-incident "why didn't it detect that?" conversations.

### 7.9 Migration Path

| Stage | Changes | Effort |
|---|---|---|
| **Demo hardening** | Health check UI, stats lock, swarm visualization | Hours |
| **Pilot** (1 site, 1 operator) | RTSP input, persistent audit log, auth | Days |
| **Small deployment** (1 site, 4 operators) | WebSocket push, alert ack workflow, RBAC, threat scoring | Weeks |
| **Multi-site** (N cameras) | Async inference queue, tracker state in Redis, horizontal scaling | Months |
| **Production robustness** | Condition-adaptive thresholds, model ensemble, difficulty estimation, cross-camera tracking | Quarters |

**The single most important architectural decision for production:** Extract tracker state from in-process memory to an external store. Everything else can be layered incrementally. In-process tracker state is the load-bearing wall that cannot be moved without rebuilding the floor above it.
