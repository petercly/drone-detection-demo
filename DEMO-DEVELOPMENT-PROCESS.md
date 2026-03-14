# Demo Development Process

A living document tracking the development journey of the Drone Detection Security Center demo, built for the Roboflow Field Engineering Tech Challenge.

---

## Phase 1: Planning & Architecture Design

### PRD-Driven Approach
Started by writing a detailed Product Requirements Document ([PRD.md](PRD.md)) before writing any code. The PRD defined:
- **Problem framing**: Security facilities lack real-time automated drone detection across multiple surveillance feeds
- **Architecture**: Flask app with 4 MJPEG video streams, Roboflow inference integration, and a custom Workflow Block for direction-of-travel tracking
- **6-phase implementation plan**: Skeleton -> Inference -> Scale to 4 feeds -> Custom Workflow Block -> Dashboard polish -> Demo prep
- **Risk identification upfront**: GIL contention with 4 threads, inference throughput at 20 req/s, browser connection limits

### Key Design Decisions
- MJPEG streaming over WebSocket/HLS for simplicity (browser renders with plain `<img>` tags)
- Server-side OpenCV overlays rather than client-side canvas drawing
- Centroid-based tracker with compass direction computation (atan2 -> 8 directions)
- Dual tracking implementation: client-side fallback (`app/tracker.py`) + Roboflow Workflow Block plugin (`drone_direction_plugin/`) to satisfy the challenge requirement while ensuring reliability

---

## Phase 2: Research & Domain Context

### Paper Integration
Referenced my NATO STO-MP-IST-210 paper ("Assuring Trustworthy Computer Vision for Rapid Counter-Drone System Testing and Deployment") to ground the demo in real operational context. Key insights extracted into [paper-reference.md](paper-reference.md):
- Even SOTA models show 60-92% missed detection rates and 65-83% false alarm rates on drone detection
- Performance cliffs under changing conditions (bright->dark, single->multiple drones)
- The ASQI framework for systematic CV evaluation maps directly to what this demo visualizes

This gives the demo a narrative arc: showing detection working is step 1; understanding *where it fails* is the harder, more valuable problem.

---

## Phase 3: Environment Setup & Dependency Troubleshooting

### Video Data Preparation
- Source: 180 MP4 files from Anti-UAV dataset (IR and EO/visual) in `/Downloads/archive/test/`
- Copied all files to `videos/`, then used ffmpeg to concatenate 45 clips each into 4 long-running videos (~35 min each): `north.mp4`, `south.mp4`, `east.mp4`, `west.mp4`
- ffmpeg had to be installed from source via Homebrew (took ~15 min to compile)

### Dependency Optimization
**Insight: `inference` vs `inference-sdk` are fundamentally different packages.**

The initial `requirements.txt` included both `inference>=0.30` and `inference-sdk>=0.30`. The `inference` package is the full ML runtime (PyTorch, ONNX, GPU support -- multi-GB install) meant to run *inside* the Docker inference server. Our Flask app only uses `inference-sdk`, a lightweight HTTP client that talks to that server.

Discovered this by tracing imports: `camera.py` only does `from inference_sdk import InferenceHTTPClient`. The `drone_direction_plugin` imports from `inference` but runs inside the Docker container's own Python environment, not ours.

**Action**: Removed `inference` from requirements.txt, cutting install from ~30+ minutes to ~2 minutes.

### NumPy/OpenCV Compatibility
Hit `numpy.core.multiarray failed to import` -- NumPy 2.x incompatible with OpenCV compiled against NumPy 1.x. Compounded by Anaconda `(base)` environment leaking into the venv (import paths going through `/anaconda3/lib/` instead of `.venv/`).

**Fix**: Used `.venv/bin/python run.py` to bypass conda entirely; pinned `numpy<2`.

### OpenCV Wheel Build
`opencv-python-headless` was building from source (very slow). Fixed with `pip install opencv-python-headless --only-binary=:all:` to force pre-built wheels.

---

## Phase 4: Server Architecture Pivot

### The GIL Starvation Problem
**Original design**: Flask dev server with `threaded=True`, 4 daemon threads running `cv2.VideoCapture.read()` + `cv2.resize()` in tight loops.

**What happened**: Browser connections stuck in `SYN_SENT` -- Flask literally could not accept TCP connections. The 4 OpenCV threads were starving the main thread of GIL time. Even reducing `PROCESS_FPS` from 5 to 2 and adding explicit `time.sleep()` yields didn't help.

**Diagnosis**: Multiple compounding issues:
1. `cv2.VideoCapture.read()` on macOS (AVFoundation) doesn't reliably release the GIL during frame decoding
2. Flask's Werkzeug dev server uses single-threaded `socket.accept()` in the main thread
3. 4 MJPEG generator threads (one per browser stream) each hold connections indefinitely
4. Without inference server running, the HTTP timeout retries added further blocking

**Pivot to gunicorn + gevent**: Switched from Flask dev server to gunicorn with gevent worker class. Gevent monkey-patches blocking calls to be cooperative, eliminating GIL contention for I/O. Required:
- `gunicorn_config.py` with `worker_class = "gevent"`, `timeout = 0` (MJPEG streams never timeout)
- Moving `start_all_feeds()` from `run.py` into app factory with lazy initialization via `@app.before_request`
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` env var to prevent macOS fork() crash with OpenCV

**Added inference guard**: Skip inference entirely when API key is unset/placeholder, preventing 4 threads from blocking on dead HTTP connections:
```python
if not ROBOFLOW_API_KEY or ROBOFLOW_API_KEY == "your_api_key_here":
    return []
```

### Port 5000 Conflict
macOS AirPlay Receiver occupies port 5000 by default. Changed to port 8000.

### Resolution: Lazy Initialization + Correct Binding
The gunicorn+gevent approach hit macOS fork() crashes with OpenCV (`objc_initializeAfterForkError`). Even with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, loopback connections timed out due to Bitdefender/NordVPN intercepting local traffic.

**What actually worked**: Reverted to Flask dev server with two key changes:
1. **Lazy feed start via `@app.before_request`** -- feeds don't start until the first HTTP request, so Flask binds and accepts connections before OpenCV threads begin consuming CPU
2. **Bind to `127.0.0.1` instead of `0.0.0.0`** -- avoids security software intercepting loopback traffic

With inference disabled (API key guard returns `[]` immediately), the feed threads are lightweight enough for Flask's threaded mode to handle.

---

## Phase 5: FPS Tuning & Performance Analysis

### The MJPEG Encoding Budget
With the architecture stabilized, the next question was FPS tuning. The original settings (`PROCESS_FPS=5`, `DISPLAY_FPS=15`) were analyzed for the older MacBook:

**Key insight**: `DISPLAY_FPS` controls JPEG encoding rate, not just display rate. At `DISPLAY_FPS=15` with 4 streams, the app encodes 60 JPEG frames/sec (4 x 15). Each 640x480 JPEG encode takes ~3-8ms, totaling ~300ms/sec of encoding work. Meanwhile `PROCESS_FPS=5` means only 5 new frames/sec per feed -- so 2/3 of encodes are duplicates of unchanged frames.

**Settings chosen for demo**: `PROCESS_FPS=5`, `DISPLAY_FPS=10`
- 10 FPS looks smooth for surveillance-style footage
- 40 encodes/sec vs 60 -- 33% less encoding pressure
- CPU stays under 70%, no thermal throttling

**Future consideration**: When real inference is added (~200-300ms per call), `PROCESS_FPS` must be capped to match inference latency (e.g., 3 FPS for 300ms inference). The 50ms minimum sleep in the process loop is a safety valve against thread spinning.

---

## Phase 6: Video Re-encoding & Dashboard Enhancement

### Video Corruption Fix
The concatenated videos showed severe smearing/artifact corruption. Root cause: `ffmpeg -c copy` (stream copy without re-encoding) mixed IR clips (640x512, grayscale) with EO/visible clips (1920x1080, color). Different codecs, resolutions, and pixel formats caused decode errors.

**Fix**: Re-encoded all 4 videos with normalization:
```
ffmpeg -f concat -safe 0 -i concat_list.txt \
  -vf "scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2,format=yuv420p" \
  -c:v libx264 -preset fast -crf 23 -an -y output.mp4
```
This forces all clips to 640x480, H.264, YUV420P -- consistent format regardless of source.

### Compass Minimap
Added an SVG compass overlay at the center of the 4-panel grid. Shows N/S/E/W triangles that light up red when drones are detected on the corresponding feed.

**CSS challenge**: Initially placed the compass inside the CSS Grid container, but it was invisible -- grid items obscured it even with `z-index: 10`. Fix: wrapped the grid in a `.grid-wrapper` with `position: relative` and made the compass a sibling of the grid (not a grid child), positioned absolutely within the wrapper.

Also enabled `TEMPLATES_AUTO_RELOAD = True` in the Flask app factory to avoid template caching issues during development (`debug=False` disables auto-reload by default).

---

## Phase 7: Roboflow Inference Integration

### Local vs. Hosted API Decision
**Original plan**: Run Roboflow inference server locally via Docker or `pip install inference`.

**Problem**: The demo laptop (Intel i7-7660U, 2-core, 16GB, no GPU) cannot run RF-DETR efficiently. Expected 1-3 seconds per frame on CPU -- with 4 feeds, that's one detection update every 4-8 seconds per feed.

**Pivot to hosted API**: Switched to `https://serverless.roboflow.com` -- Roboflow's serverless inference endpoint. The Flask app sends frames over the network and receives results. This offloads all ML compute to Roboflow's cloud.

**Config change**: `INFERENCE_SERVER_URL` went from `http://localhost:9001` to `https://serverless.roboflow.com`. `PROCESS_FPS` dropped from 5 to 2 to account for network latency (~300-800ms per call).

### Custom Workflow Block: Quadrant Mapper
Built a custom Workflow Block in Roboflow's visual editor to satisfy the challenge's "custom Workflow Block" requirement. The block takes batch detection predictions (`object_detection_prediction`) and computes:

1. **Per-drone centroid** from bounding box coordinates
2. **Quadrant mapping** on a 3x3 grid (1-9) overlaid on the 640x480 frame
3. **Swarm centroid** -- average position of all detected drones (centre of mass)
4. **Swarm quadrant** -- which grid cell the swarm centre falls in

```
Quadrant layout:
  1 (TL) | 2 (TC) | 3 (TR)
  4 (ML) | 5 (MC) | 6 (MR)
  7 (BL) | 8 (BC) | 9 (BR)
```

**Design rationale**: The Workflow Block handles per-frame spatial reasoning ("where is each drone now?"). The client-side tracker handles temporal reasoning ("where is it going?") by tracking how each drone's quadrant changes over time. E.g., sequence `1→2→3` = left-to-right movement, contextualized per camera orientation.

### Workflow Debugging
Several iterations to get the custom block working:
- **Indent error**: Roboflow's code validator rejected leading whitespace on the first line. Had to ensure code started flush-left.
- **Parameter naming**: Python doesn't allow dots in parameter names, but Roboflow's workflow wiring uses `model.predictions`. Renamed to `model_predictions` and mapped via the block's input configuration.
- **Input kind**: Initially wired as `detection` (single bbox per call). Changed to `object_detection_prediction` (batch of all detections) to enable swarm centroid computation across all drones in a frame.

### First Successful Inference
Tested the workflow end-to-end: extracted a frame from source video, sent to the hosted API, received detection results with bounding box coordinates and object count. The existing `camera.py` parsing logic handled the response format correctly.

### Direction Tracking: atan2 vs Quadrant Sequence
Evaluated two approaches for direction-of-travel computation with a tech-lead-challenger review:

1. **atan2 centroid tracker (chosen)**: Client-side, uses sliding window of centroid positions, continuous angle → 8 compass directions. More accurate, handles slow-moving drones, already implemented and working.

2. **Quadrant-sequence tracking (rejected)**: Would track how each drone's 3x3 grid cell changes over time. Too coarse (9 cells), boundary oscillation issues, and still requires a client-side tracker — would be rebuilding a worse version of what already exists.

**Decision**: Keep atan2 for direction. Use the Workflow Block's quadrant output as backend input for intercardinal blind-spot warnings instead of displaying it directly.

---

## Phase 8: Intercardinal Blind-Spot Warnings

### The Blind-Spot Problem
Four cameras covering N/S/E/W leave diagonal gaps (NE/SE/SW/NW). Drones moving horizontally across a camera's field of view are heading toward one of these blind spots.

### Implementation
- Track horizontal centroid displacement over the tracker's sliding window
- Map horizontal movement direction per camera to intercardinal direction:
  - NORTH cam: left→right = NE warning, right→left = NW warning
  - SOUTH cam: left→right = SW, right→left = SE
  - EAST cam: left→right = SE, right→left = NE
  - WEST cam: left→right = NW, right→left = SW
- Threshold: >30px horizontal displacement to trigger warning
- Dashboard compass expanded with 4 intercardinal triangles (NE/SE/SW/NW)
- Cardinal triangles light up **red** (drone detected), intercardinal light up **yellow** (blind-spot warning)

### PRD Alignment
Updated PRD.md to reflect actual delivered architecture: hosted API, atan2 direction tracker, quadrant mapper Workflow Block, intercardinal warnings, dual tracking rationale.

---

## Architecture Evolution Summary

```
Original Design          ->  Current Design
─────────────────────         ─────────────────────
Flask dev server              Flask dev server (threaded)
start_all_feeds() in main     Lazy start via @before_request
host 0.0.0.0                  host 127.0.0.1
Port 5000                     Port 8000
Inference always called       Guard: skip if no API key
Local inference server        Roboflow hosted serverless API
Direction via Workflow Block   Direction via client-side atan2
No blind-spot warnings        Intercardinal yellow warnings
PROCESS_FPS=5/DISPLAY_FPS=15  PROCESS_FPS=2/DISPLAY_FPS=10
requirements: inference        requirements: inference-sdk only
  (full ML runtime)             (lightweight HTTP client)
ffmpeg -c copy (corrupted)    ffmpeg re-encode (normalized)
No compass minimap            SVG compass: 4 cardinal + 4 intercardinal
```

---

## Tools & Workflow

- **Claude Code**: Used as pair-programming partner throughout -- PRD drafting, code generation, dependency diagnosis, architecture decisions, and real-time troubleshooting
- **Tech-lead-challenger agent**: Custom Claude agent providing senior engineering perspective on trade-offs, diagnosing the GIL starvation root cause, and recommending the gunicorn+gevent approach (which led to discovering the real fix was simpler)
- **Roboflow Workflow Editor**: Visual workflow builder for chaining detection model + custom block + visualization
- **ffmpeg**: Video concatenation and re-encoding for demo-length feeds
- **Activity Monitor + lsof**: Process and port diagnostics during server debugging

---

*Last updated: Phase 8 -- Intercardinal blind-spot warnings implemented, PRD aligned with delivered architecture, initial commit to GitHub*
