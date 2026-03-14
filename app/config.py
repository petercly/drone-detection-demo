import os
from dotenv import load_dotenv

load_dotenv()

ROBOFLOW_API_KEY = os.getenv("ROBOFLOW_API_KEY", "")
ROBOFLOW_WORKSPACE = os.getenv("ROBOFLOW_WORKSPACE", "")
ROBOFLOW_PROJECT = os.getenv("ROBOFLOW_PROJECT", "")
ROBOFLOW_MODEL_VERSION = os.getenv("ROBOFLOW_MODEL_VERSION", "1")
INFERENCE_SERVER_URL = os.getenv("INFERENCE_SERVER_URL", "http://localhost:9001")

PROCESS_FPS = int(os.getenv("PROCESS_FPS", "5"))
DISPLAY_FPS = int(os.getenv("DISPLAY_FPS", "15"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_DIR = os.path.join(BASE_DIR, "videos")

FEEDS = {
    "north": os.path.join(VIDEO_DIR, "north.mp4"),
    "south": os.path.join(VIDEO_DIR, "south.mp4"),
    "east": os.path.join(VIDEO_DIR, "east.mp4"),
    "west": os.path.join(VIDEO_DIR, "west.mp4"),
}

# Tracker settings
MAX_DISTANCE_THRESHOLD = 100  # pixels
MAX_FRAMES_MISSING = 5
DIRECTION_WINDOW = 10  # frames of centroid history for direction calc
STATIONARY_THRESHOLD = 15  # pixels - below this displacement, "Stationary"

# Detection confidence threshold
CONFIDENCE_THRESHOLD = 0.3

# Workflow ID (set after creating workflow in Roboflow)
WORKFLOW_ID = os.getenv("WORKFLOW_ID", "")
