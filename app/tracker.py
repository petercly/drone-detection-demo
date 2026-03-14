"""Centroid-based object tracker for drone direction-of-travel computation."""

import math
from collections import OrderedDict, deque

import numpy as np
from scipy.spatial.distance import cdist

from .config import (
    DIRECTION_WINDOW,
    MAX_DISTANCE_THRESHOLD,
    MAX_FRAMES_MISSING,
    STATIONARY_THRESHOLD,
)

COMPASS_DIRECTIONS = [
    "N", "NE", "E", "SE", "S", "SW", "W", "NW"
]


def vector_to_compass(dx, dy):
    """Convert a displacement vector to one of 8 compass directions.

    Note: In image coordinates, +y is downward, so we negate dy for compass.
    """
    if abs(dx) < STATIONARY_THRESHOLD and abs(dy) < STATIONARY_THRESHOLD:
        return "Stationary"
    angle = math.atan2(-dy, dx)  # negate dy because image y-axis is inverted
    angle_deg = math.degrees(angle)
    # Rotate so 0° = North (up), and bin into 8 directions
    # atan2 gives 0° = East, 90° = North (after negation)
    # Shift: North = 90° in atan2 -> index 0
    index = round((90 - angle_deg) / 45) % 8
    return COMPASS_DIRECTIONS[index]


class CentroidTracker:
    """Tracks objects across frames using centroid distance matching."""

    def __init__(self):
        self.next_id = 0
        self.objects = OrderedDict()       # id -> current centroid (cx, cy)
        self.histories = OrderedDict()     # id -> deque of centroids
        self.disappeared = OrderedDict()   # id -> frames since last seen
        self.directions = OrderedDict()    # id -> current direction string
        self.total_unique = 0              # total unique objects ever tracked

    def reset(self):
        self.__init__()

    def _register(self, centroid):
        obj_id = self.next_id
        self.objects[obj_id] = centroid
        self.histories[obj_id] = deque([centroid], maxlen=DIRECTION_WINDOW)
        self.disappeared[obj_id] = 0
        self.directions[obj_id] = "Stationary"
        self.next_id += 1
        self.total_unique += 1
        return obj_id

    def _deregister(self, obj_id):
        del self.objects[obj_id]
        del self.histories[obj_id]
        del self.disappeared[obj_id]
        del self.directions[obj_id]

    def _compute_direction(self, obj_id):
        history = self.histories[obj_id]
        if len(history) < 2:
            return "Stationary"
        oldest = history[0]
        newest = history[-1]
        dx = newest[0] - oldest[0]
        dy = newest[1] - oldest[1]
        return vector_to_compass(dx, dy)

    def update(self, detections):
        """Update tracker with new detections.

        Args:
            detections: list of dicts with keys 'x', 'y', 'width', 'height'
                        (center coordinates and dimensions)

        Returns:
            dict: {object_id: {'centroid': (cx, cy), 'direction': str}}
        """
        # Compute centroids from detections
        input_centroids = []
        for det in detections:
            cx = det["x"]
            cy = det["y"]
            input_centroids.append((cx, cy))

        # No existing objects - register all
        if len(self.objects) == 0:
            for centroid in input_centroids:
                self._register(centroid)
        # No new detections - mark all as disappeared
        elif len(input_centroids) == 0:
            for obj_id in list(self.disappeared.keys()):
                self.disappeared[obj_id] += 1
                if self.disappeared[obj_id] > MAX_FRAMES_MISSING:
                    self._deregister(obj_id)
        # Match existing objects to new detections
        else:
            obj_ids = list(self.objects.keys())
            obj_centroids = list(self.objects.values())

            D = cdist(np.array(obj_centroids), np.array(input_centroids))

            rows = D.min(axis=1).argsort()
            cols = D.argmin(axis=1)[rows]

            used_rows = set()
            used_cols = set()

            for row, col in zip(rows, cols):
                if row in used_rows or col in used_cols:
                    continue
                if D[row, col] > MAX_DISTANCE_THRESHOLD:
                    continue

                obj_id = obj_ids[row]
                self.objects[obj_id] = input_centroids[col]
                self.histories[obj_id].append(input_centroids[col])
                self.disappeared[obj_id] = 0
                self.directions[obj_id] = self._compute_direction(obj_id)

                used_rows.add(row)
                used_cols.add(col)

            unused_rows = set(range(D.shape[0])) - used_rows
            unused_cols = set(range(D.shape[1])) - used_cols

            # Handle disappeared existing objects
            for row in unused_rows:
                obj_id = obj_ids[row]
                self.disappeared[obj_id] += 1
                if self.disappeared[obj_id] > MAX_FRAMES_MISSING:
                    self._deregister(obj_id)

            # Register new detections
            for col in unused_cols:
                self._register(input_centroids[col])

        # Build result
        result = {}
        for obj_id in self.objects:
            result[obj_id] = {
                "centroid": self.objects[obj_id],
                "direction": self.directions[obj_id],
            }
        return result
