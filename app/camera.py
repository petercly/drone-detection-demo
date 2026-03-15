"""Camera feed processing - reads video, runs inference, draws overlays."""

import threading
import time
from collections import deque

import cv2
import numpy as np
from inference_sdk import InferenceHTTPClient

from .config import (
    CONFIDENCE_THRESHOLD,
    DISPLAY_FPS,
    FEEDS,
    INFERENCE_SERVER_URL,
    PROCESS_FPS,
    ROBOFLOW_API_KEY,
    ROBOFLOW_MODEL_VERSION,
    ROBOFLOW_PROJECT,
    ROBOFLOW_WORKSPACE,
    WORKFLOW_ID,
)
from .tracker import CentroidTracker

# Global registry of active feeds
feeds = {}

# Global alert event log
alert_log = deque(maxlen=50)


class CameraFeed:
    """Processes a single video feed with detection and tracking."""

    def __init__(self, name, video_path):
        self.name = name
        self.video_path = video_path
        self.lock = threading.Lock()
        self.frame = None
        self.running = False
        self.tracker = CentroidTracker()

        # Stats
        self.drone_count = 0
        self.directions = {}
        self.alert = False
        self.total_unique = 0
        self.inference_enabled = False

        # Alert debouncing
        self._alert_on_count = 0
        self._alert_off_count = 0

        # Quadrant data from workflow (3x3 grid, sectors 1-9)
        self.quadrants = []
        self.swarm_quadrant = None
        self.intercardinal_warnings = []
        self.quadrant_history = deque(maxlen=200)  # (timestamp, set_of_occupied_quadrants)

        # Inference client
        self.client = InferenceHTTPClient(
            api_url=INFERENCE_SERVER_URL,
            api_key=ROBOFLOW_API_KEY,
        )

        self.model_id = f"{ROBOFLOW_PROJECT}/{ROBOFLOW_MODEL_VERSION}"

    def start(self):
        self.running = True
        thread = threading.Thread(target=self._process_loop, daemon=True)
        thread.start()

    def stop(self):
        self.running = False

    def get_frame(self):
        with self.lock:
            return self.frame

    def get_stats(self):
        return {
            "name": self.name,
            "drone_count": self.drone_count,
            "directions": self.directions,
            "alert": self.alert,
            "total_unique": self.total_unique,
            "intercardinal_warnings": self.intercardinal_warnings,
        }

    def _process_loop(self):
        frame_interval = 1.0 / PROCESS_FPS

        while self.running:
            cap = cv2.VideoCapture(self.video_path)
            if not cap.isOpened():
                print(f"[{self.name}] Cannot open {self.video_path}, retrying in 5s...")
                self._set_error_frame()
                time.sleep(5)
                continue

            while self.running:
                loop_start = time.time()

                ret, frame = cap.read()
                if not ret:
                    # Loop video
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret:
                        break
                    self.tracker.reset()
                    self._alert_on_count = 0
                    self._alert_off_count = 0

                # Resize for consistent display
                frame = cv2.resize(frame, (640, 480))

                # Run inference
                detections = self._run_inference(frame)

                # Update tracker
                tracked = self.tracker.update(detections)

                # Update stats
                self.drone_count = len(tracked)
                self.total_unique = self.tracker.total_unique

                # Debounced alert: require consecutive frames to trigger/clear
                prev_alert = self.alert
                if self.drone_count > 0:
                    self._alert_on_count += 1
                    self._alert_off_count = 0
                    if self._alert_on_count >= 3:
                        self.alert = True
                else:
                    self._alert_off_count += 1
                    self._alert_on_count = 0
                    if self._alert_off_count >= 5:
                        self.alert = False

                # Log alert state transitions
                if self.alert != prev_alert:
                    alert_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "feed": self.name.upper(),
                        "event": "DETECTED" if self.alert else "CLEARED",
                        "count": self.drone_count,
                    })
                self.directions = {
                    str(oid): self._screen_to_world_direction(info["direction"])
                    for oid, info in tracked.items()
                }

                # Compute intercardinal blind-spot warnings from quadrant transitions
                self._update_intercardinal_warnings(detections, tracked)

                # Draw overlays
                annotated = self._draw_overlays(frame, tracked)

                with self.lock:
                    self.frame = annotated

                # Maintain target FPS and yield to other threads
                elapsed = time.time() - loop_start
                sleep_time = max(frame_interval - elapsed, 0.05)
                time.sleep(sleep_time)

            cap.release()

            # If still running, reconnect after brief pause
            if self.running:
                print(f"[{self.name}] Feed ended, reconnecting...")
                self.tracker.reset()
                time.sleep(1)

    def _run_inference(self, frame):
        """Run object detection via Roboflow inference."""
        if not self.inference_enabled:
            return []
        if not ROBOFLOW_API_KEY or ROBOFLOW_API_KEY == "your_api_key_here":
            return []
        try:
            if WORKFLOW_ID:
                result = self.client.run_workflow(
                    workspace_name=ROBOFLOW_WORKSPACE,
                    workflow_id=WORKFLOW_ID,
                    images={"image": frame},
                    use_cache=True,
                )
                return self._parse_workflow_result(result)
            else:
                result = self.client.infer(frame, model_id=self.model_id)
                return self._parse_detection_result(result)
        except Exception as e:
            print(f"[{self.name}] Inference error: {e}")
            return []

    def _parse_detection_result(self, result):
        """Parse standard detection result into list of detection dicts."""
        detections = []
        predictions = result.get("predictions", [])
        for pred in predictions:
            if pred.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            detections.append({
                "x": pred["x"],
                "y": pred["y"],
                "width": pred["width"],
                "height": pred["height"],
                "confidence": pred["confidence"],
                "class": pred.get("class", "drone"),
            })
        return detections

    # Maps camera name + horizontal movement to intercardinal direction.
    # "right" = drone detected in right-edge quadrants (3,6,9).
    # "left" = drone detected in left-edge quadrants (1,4,7).
    INTERCARDINAL_MAP = {
        "north": {"right": "NE", "left": "NW"},
        "south": {"right": "SW", "left": "SE"},
        "east":  {"right": "SE", "left": "NE"},
        "west":  {"right": "NW", "left": "SW"},
    }

    # Quadrant column groupings for center→edge transition detection
    QUADRANT_MEMORY_WINDOW = 15.0  # seconds
    LEFT_EDGE_QUADRANTS = {1, 4, 7}
    CENTER_QUADRANTS = {2, 5, 8}
    RIGHT_EDGE_QUADRANTS = {3, 6, 9}
    RIGHTWARD_DIRS = {"E", "NE", "SE"}
    LEFTWARD_DIRS = {"W", "NW", "SW"}

    # Screen-space to world-space direction mapping per camera.
    # Cameras face outward; horizontal screen movement maps to world directions.
    # Vertical screen movement is elevation change, not cardinal direction.
    SCREEN_TO_WORLD = {
        "north": {"right": "E", "left": "W"},
        "south": {"right": "W", "left": "E"},
        "east":  {"right": "S", "left": "N"},
        "west":  {"right": "N", "left": "S"},
    }

    def _screen_to_world_direction(self, screen_dir):
        """Convert screen-space direction to world-space direction.

        Horizontal screen movement maps to world cardinal directions per camera.
        Pure vertical movement (N/S on screen) is elevation, mapped to Stationary.
        """
        if screen_dir == "Stationary":
            return "Stationary"
        mapping = self.SCREEN_TO_WORLD.get(self.name, {})
        if not mapping:
            return screen_dir
        if screen_dir in self.RIGHTWARD_DIRS:
            return mapping["right"]
        if screen_dir in self.LEFTWARD_DIRS:
            return mapping["left"]
        # Pure vertical (N or S on screen) = elevation change, not lateral
        return "Stationary"

    def _parse_workflow_result(self, result):
        """Parse workflow result into detection dicts and quadrant data."""
        detections = []
        if not result or not isinstance(result, list):
            return detections
        # Workflow returns list of outputs; first element contains predictions
        output = result[0] if result else {}
        predictions = output.get("predictions", output.get("output", []))
        if isinstance(predictions, dict):
            predictions = predictions.get("predictions", [])
        for pred in predictions:
            if pred.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            detections.append({
                "x": pred.get("x", 0),
                "y": pred.get("y", 0),
                "width": pred.get("width", 0),
                "height": pred.get("height", 0),
                "confidence": pred.get("confidence", 0),
                "class": pred.get("class", "drone"),
            })

        # Extract quadrant data from custom workflow block
        self.quadrants = output.get("quadrants", [])
        self.swarm_quadrant = output.get("swarm_quadrant", None)

        return detections

    @staticmethod
    def _centroid_to_quadrant(x, y, img_w=640, img_h=480):
        """Map a detection centroid (x, y) to a 3x3 grid quadrant (1-9).

        Layout:  1 (TL) | 2 (TC) | 3 (TR)
                 4 (ML) | 5 (MC) | 6 (MR)
                 7 (BL) | 8 (BC) | 9 (BR)
        """
        col = min(int(x / (img_w / 3)), 2)
        row = min(int(y / (img_h / 3)), 2)
        return row * 3 + col + 1

    def _update_intercardinal_warnings(self, detections, tracked):
        """Derive intercardinal compass warnings from quadrant analysis.

        Two trigger conditions (OR logic):
        1. Center→edge quadrant transition within the memory window.
        2. Drone currently in an edge quadrant with atan2 direction pointing
           toward that edge (e.g., in right-edge quadrant heading E/NE/SE).
        """
        now = time.time()

        # Compute current frame's occupied quadrants
        current_quads = set()
        for det in detections:
            q = self._centroid_to_quadrant(det["x"], det["y"])
            current_quads.add(q)

        # Record in history
        if current_quads:
            self.quadrant_history.append((now, current_quads))

        # Prune entries older than the memory window
        cutoff = now - self.QUADRANT_MEMORY_WINDOW
        while self.quadrant_history and self.quadrant_history[0][0] < cutoff:
            self.quadrant_history.popleft()

        # Collect all quadrants seen in recent history
        recent_quads = set()
        for _, quads in self.quadrant_history:
            recent_quads.update(quads)

        # Check for center→edge transition
        had_center = bool(recent_quads & self.CENTER_QUADRANTS)
        warnings = set()
        mapping = self.INTERCARDINAL_MAP.get(self.name, {})

        if had_center and mapping:
            if current_quads & self.RIGHT_EDGE_QUADRANTS:
                warnings.add(mapping["right"])
            if current_quads & self.LEFT_EDGE_QUADRANTS:
                warnings.add(mapping["left"])

        # OR: drone in edge quadrant with atan2 direction pointing toward that edge
        if mapping:
            for obj_id, info in tracked.items():
                cx, cy = info["centroid"]
                direction = info["direction"]
                q = self._centroid_to_quadrant(cx, cy)
                if q in self.RIGHT_EDGE_QUADRANTS and direction in self.RIGHTWARD_DIRS:
                    warnings.add(mapping["right"])
                if q in self.LEFT_EDGE_QUADRANTS and direction in self.LEFTWARD_DIRS:
                    warnings.add(mapping["left"])

        self.intercardinal_warnings = list(warnings)

    def _draw_overlays(self, frame, tracked):
        """Draw bounding boxes, direction arrows, and status overlays."""
        annotated = frame.copy()

        for obj_id, info in tracked.items():
            cx, cy = info["centroid"]
            cx, cy = int(cx), int(cy)
            screen_dir = info["direction"]
            world_dir = self._screen_to_world_direction(screen_dir)
            confidence = info.get("confidence", 0)

            # Draw bounding box (estimated from centroid)
            half_w, half_h = 40, 30
            x1, y1 = cx - half_w, cy - half_h
            x2, y2 = cx + half_w, cy + half_h

            # Hovering drones get amber color, others green
            is_hovering = world_dir == "Stationary"
            color = (0, 165, 255) if is_hovering else (0, 255, 0)

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

            # ID, world-space direction, and confidence label
            dir_label = "HOVER" if is_hovering else world_dir
            label = f"ID:{obj_id} {dir_label} {confidence:.0%}"
            cv2.putText(
                annotated, label, (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2,
            )

            # Direction arrow (screen-space) or hover indicator
            if is_hovering:
                # Concentric circles for hovering drone
                cv2.circle(annotated, (cx, cy), 20, (0, 165, 255), 1)
                cv2.circle(annotated, (cx, cy), 30, (0, 165, 255), 1)
            else:
                arrow_len = 40
                dx, dy = self._direction_to_vector(screen_dir)
                end_x = cx + int(dx * arrow_len)
                end_y = cy + int(dy * arrow_len)
                cv2.arrowedLine(
                    annotated, (cx, cy), (end_x, end_y),
                    (0, 255, 255), 2, tipLength=0.3,
                )

        # Camera label
        label_text = self.name.upper()
        cv2.putText(
            annotated, label_text, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
        )

        # Drone count
        count_text = f"Drones: {self.drone_count}"
        cv2.putText(
            annotated, count_text, (10, 470),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2,
        )

        # Alert badge
        if self.alert:
            cv2.putText(
                annotated, "ALERT", (540, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2,
            )
            # Red border
            cv2.rectangle(annotated, (0, 0), (639, 479), (0, 0, 255), 3)

        return annotated

    def _direction_to_vector(self, direction):
        """Convert compass direction to unit vector (image coords: +y is down)."""
        vectors = {
            "N": (0, -1), "NE": (0.7, -0.7), "E": (1, 0), "SE": (0.7, 0.7),
            "S": (0, 1), "SW": (-0.7, 0.7), "W": (-1, 0), "NW": (-0.7, -0.7),
        }
        return vectors.get(direction, (0, 0))

    def _set_error_frame(self):
        """Set a placeholder error frame."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame, f"{self.name.upper()}: NO VIDEO",
            (120, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2,
        )
        with self.lock:
            self.frame = frame


def start_all_feeds():
    """Initialize and start all camera feeds."""
    for name, path in FEEDS.items():
        feed = CameraFeed(name, path)
        feeds[name] = feed
        feed.start()
        print(f"[*] Started feed: {name} -> {path}")


def enable_inference():
    """Enable inference on all active feeds."""
    for feed in feeds.values():
        feed.inference_enabled = True
        feed.tracker.reset()
    print("[*] Inference enabled on all feeds")


def disable_inference():
    """Disable inference on all active feeds and reset state."""
    for feed in feeds.values():
        feed.inference_enabled = False
        feed.tracker.reset()
        feed.drone_count = 0
        feed.alert = False
        feed.directions = {}
        feed.intercardinal_warnings = []
        feed.quadrant_history.clear()
        feed._alert_on_count = 0
        feed._alert_off_count = 0
    print("[*] Inference disabled on all feeds")
