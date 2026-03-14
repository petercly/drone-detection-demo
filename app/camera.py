"""Camera feed processing - reads video, runs inference, draws overlays."""

import threading
import time

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

        # Quadrant data from workflow (3x3 grid, sectors 1-9)
        self.quadrants = []
        self.swarm_quadrant = None
        self.intercardinal_warnings = []

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
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            print(f"[{self.name}] ERROR: Cannot open {self.video_path}")
            self._set_error_frame()
            return

        frame_interval = 1.0 / PROCESS_FPS

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

            # Resize for consistent display
            frame = cv2.resize(frame, (640, 480))

            # Run inference
            detections = self._run_inference(frame)

            # Update tracker
            tracked = self.tracker.update(detections)

            # Update stats
            self.drone_count = len(tracked)
            self.alert = self.drone_count > 0
            self.total_unique = self.tracker.total_unique
            self.directions = {
                str(oid): info["direction"] for oid, info in tracked.items()
            }

            # Compute intercardinal blind-spot warnings from horizontal movement
            self._update_intercardinal_warnings()

            # Draw overlays
            annotated = self._draw_overlays(frame, tracked)

            with self.lock:
                self.frame = annotated

            # Maintain target FPS and yield to other threads
            elapsed = time.time() - loop_start
            sleep_time = max(frame_interval - elapsed, 0.05)
            time.sleep(sleep_time)

        cap.release()

    def _run_inference(self, frame):
        """Run object detection via Roboflow inference."""
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
    # "right" = drone moving left-to-right in frame (toward higher column).
    # "left" = drone moving right-to-left in frame (toward lower column).
    INTERCARDINAL_MAP = {
        "north": {"right": "NE", "left": "NW"},
        "south": {"right": "SW", "left": "SE"},
        "east":  {"right": "SE", "left": "NE"},
        "west":  {"right": "NW", "left": "SW"},
    }

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

    def _update_intercardinal_warnings(self):
        """Derive intercardinal compass warnings from horizontal quadrant movement.

        Compares each drone's current quadrant column to the tracker's centroid
        history to detect horizontal movement across the frame. A drone moving
        left-to-right or right-to-left indicates it's heading toward a camera
        blind spot (the gap between two cardinal cameras), which maps to an
        intercardinal direction (NW/NE/SW/SE) depending on which camera sees it.
        """
        warnings = set()
        mapping = self.INTERCARDINAL_MAP.get(self.name, {})
        if not mapping:
            self.intercardinal_warnings = []
            return

        for oid, info in self.tracker.objects.items():
            history = self.tracker.histories.get(oid)
            if not history or len(history) < 3:
                continue
            # Compute horizontal displacement from centroid history
            oldest_x = history[0][0]
            newest_x = history[-1][0]
            dx = newest_x - oldest_x
            # Threshold: at least 30px horizontal movement to trigger warning
            if dx > 30:
                warnings.add(mapping["right"])
            elif dx < -30:
                warnings.add(mapping["left"])

        self.intercardinal_warnings = list(warnings)

    def _draw_overlays(self, frame, tracked):
        """Draw bounding boxes, direction arrows, and status overlays."""
        annotated = frame.copy()

        for obj_id, info in tracked.items():
            cx, cy = info["centroid"]
            cx, cy = int(cx), int(cy)
            direction = info["direction"]

            # Draw bounding box (estimated from centroid)
            half_w, half_h = 40, 30
            x1, y1 = cx - half_w, cy - half_h
            x2, y2 = cx + half_w, cy + half_h

            # Green box
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # ID and direction label
            label = f"ID:{obj_id} {direction}"
            cv2.putText(
                annotated, label, (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2,
            )

            # Direction arrow
            if direction != "Stationary":
                arrow_len = 40
                dx, dy = self._direction_to_vector(direction)
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
