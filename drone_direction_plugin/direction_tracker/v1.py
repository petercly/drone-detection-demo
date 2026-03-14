"""
Custom Roboflow Workflow Block: Direction-of-Travel Tracker

Computes the compass direction of travel for each detected object by tracking
centroids across frames using nearest-neighbor matching.

This block is designed to be used in a Roboflow Workflow after an object
detection model step. It takes detection predictions and enriches them
with tracker IDs and direction-of-travel information.
"""

import math
from collections import OrderedDict, deque
from typing import Dict, List, Literal, Optional, Tuple, Type, Union

import numpy as np
from inference.core.workflows.execution_engine.entities.base import (
    OutputDefinition,
    WorkflowImageData,
)
from inference.core.workflows.execution_engine.entities.types import (
    OBJECT_DETECTION_PREDICTION_KIND,
    StepOutputSelector,
    WorkflowParameterSelector,
)
from inference.core.workflows.prototypes.block import (
    BlockResult,
    WorkflowBlock,
    WorkflowBlockManifest,
)


COMPASS_DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

SHORT_DESCRIPTION = "Track detected objects and compute their compass direction of travel"
LONG_DESCRIPTION = """
The Direction-of-Travel Tracker block takes object detection predictions and:
1. Assigns persistent tracker IDs using centroid-based nearest-neighbor matching
2. Maintains a sliding window of centroid positions per tracked object
3. Computes the compass direction of travel (N/NE/E/SE/S/SW/W/NW) from the
   displacement vector over the sliding window
4. Outputs enriched predictions with tracker_id and direction fields,
   plus a summary of all active directions and unique object count
"""


class DirectionOfTravelManifest(WorkflowBlockManifest):
    model_config = {
        "json_schema_extra": {
            "name": "Direction of Travel Tracker",
            "version": "v1",
            "short_description": SHORT_DESCRIPTION,
            "long_description": LONG_DESCRIPTION,
            "license": "MIT",
            "block_type": "transformation",
        }
    }
    type: Literal["roboflow_core/direction_of_travel_tracker@v1"]
    predictions: StepOutputSelector(
        kind=[OBJECT_DETECTION_PREDICTION_KIND]
    )
    max_distance: Union[int, WorkflowParameterSelector(kind=[])] = 100
    max_frames_missing: Union[int, WorkflowParameterSelector(kind=[])] = 5
    history_window: Union[int, WorkflowParameterSelector(kind=[])] = 10
    stationary_threshold: Union[int, WorkflowParameterSelector(kind=[])] = 15

    @classmethod
    def describe_outputs(cls) -> List[OutputDefinition]:
        return [
            OutputDefinition(name="tracked_predictions", kind=[]),
            OutputDefinition(name="direction_summary", kind=[]),
            OutputDefinition(name="unique_count", kind=[]),
        ]

    @classmethod
    def get_execution_engine_compatibility(cls) -> Optional[str]:
        return ">=1.0.0,<2.0.0"


class DirectionOfTravelBlockV1(WorkflowBlock):
    """Workflow block that tracks objects and computes direction of travel."""

    def __init__(self):
        self._next_id = 0
        self._objects: OrderedDict = OrderedDict()
        self._histories: OrderedDict = OrderedDict()
        self._disappeared: OrderedDict = OrderedDict()
        self._total_unique = 0

    @classmethod
    def get_manifest(cls) -> Type[WorkflowBlockManifest]:
        return DirectionOfTravelManifest

    def run(
        self,
        predictions: dict,
        max_distance: int = 100,
        max_frames_missing: int = 5,
        history_window: int = 10,
        stationary_threshold: int = 15,
    ) -> BlockResult:
        # Extract detections
        dets = predictions.get("predictions", [])

        # Compute centroids
        input_centroids = []
        for det in dets:
            cx = det.get("x", 0)
            cy = det.get("y", 0)
            input_centroids.append((cx, cy))

        # Update tracking
        assignments = self._update_tracking(
            input_centroids, max_distance, max_frames_missing, history_window
        )

        # Build enriched predictions
        tracked_predictions = []
        direction_summary = {}

        for i, det in enumerate(dets):
            centroid = input_centroids[i]
            # Find which tracker ID was assigned to this detection
            tracker_id = assignments.get(i)
            if tracker_id is not None:
                direction = self._compute_direction(
                    tracker_id, history_window, stationary_threshold
                )
                enriched = dict(det)
                enriched["tracker_id"] = tracker_id
                enriched["direction"] = direction
                tracked_predictions.append(enriched)
                direction_summary[str(tracker_id)] = direction

        return {
            "tracked_predictions": tracked_predictions,
            "direction_summary": direction_summary,
            "unique_count": self._total_unique,
        }

    def _update_tracking(
        self,
        input_centroids: List[Tuple[float, float]],
        max_distance: int,
        max_frames_missing: int,
        history_window: int,
    ) -> Dict[int, int]:
        """Match input centroids to existing tracks. Returns {detection_idx: tracker_id}."""
        assignments = {}

        if len(self._objects) == 0:
            for i, c in enumerate(input_centroids):
                tid = self._register(c, history_window)
                assignments[i] = tid
            return assignments

        if len(input_centroids) == 0:
            for obj_id in list(self._disappeared.keys()):
                self._disappeared[obj_id] += 1
                if self._disappeared[obj_id] > max_frames_missing:
                    self._deregister(obj_id)
            return assignments

        obj_ids = list(self._objects.keys())
        obj_centroids = np.array(list(self._objects.values()))
        inp_centroids = np.array(input_centroids)

        # Distance matrix
        D = np.linalg.norm(
            obj_centroids[:, np.newaxis] - inp_centroids[np.newaxis, :], axis=2
        )

        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]

        used_rows = set()
        used_cols = set()

        for row, col in zip(rows, cols):
            if row in used_rows or col in used_cols:
                continue
            if D[row, col] > max_distance:
                continue

            obj_id = obj_ids[row]
            self._objects[obj_id] = input_centroids[col]
            self._histories[obj_id].append(input_centroids[col])
            self._disappeared[obj_id] = 0
            assignments[col] = obj_id

            used_rows.add(row)
            used_cols.add(col)

        for row in set(range(len(obj_ids))) - used_rows:
            obj_id = obj_ids[row]
            self._disappeared[obj_id] += 1
            if self._disappeared[obj_id] > max_frames_missing:
                self._deregister(obj_id)

        for col in set(range(len(input_centroids))) - used_cols:
            tid = self._register(input_centroids[col], history_window)
            assignments[col] = tid

        return assignments

    def _register(self, centroid, history_window):
        obj_id = self._next_id
        self._objects[obj_id] = centroid
        self._histories[obj_id] = deque([centroid], maxlen=history_window)
        self._disappeared[obj_id] = 0
        self._next_id += 1
        self._total_unique += 1
        return obj_id

    def _deregister(self, obj_id):
        del self._objects[obj_id]
        del self._histories[obj_id]
        del self._disappeared[obj_id]

    def _compute_direction(self, obj_id, history_window, stationary_threshold):
        history = self._histories.get(obj_id)
        if not history or len(history) < 2:
            return "Stationary"
        oldest = history[0]
        newest = history[-1]
        dx = newest[0] - oldest[0]
        dy = newest[1] - oldest[1]
        if abs(dx) < stationary_threshold and abs(dy) < stationary_threshold:
            return "Stationary"
        angle = math.atan2(-dy, dx)
        angle_deg = math.degrees(angle)
        index = round((90 - angle_deg) / 45) % 8
        return COMPASS_DIRECTIONS[index]
