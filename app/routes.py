"""Flask routes for MJPEG streaming and stats API."""

import time

import cv2
from flask import Blueprint, Response, jsonify, render_template

from .camera import feeds
from .config import DISPLAY_FPS

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    return render_template("dashboard.html")


@main_bp.route("/video_feed/<name>")
def video_feed(name):
    if name not in feeds:
        return "Feed not found", 404
    return Response(
        _generate_mjpeg(name),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@main_bp.route("/api/stats")
def api_stats():
    all_stats = {}
    total_drones = 0
    any_alert = False
    total_unique = 0

    for name, feed in feeds.items():
        stats = feed.get_stats()
        all_stats[name] = stats
        total_drones += stats["drone_count"]
        total_unique += stats["total_unique"]
        if stats["alert"]:
            any_alert = True

    return jsonify({
        "feeds": all_stats,
        "total_drones": total_drones,
        "total_unique": total_unique,
        "any_alert": any_alert,
    })


def _generate_mjpeg(name):
    """Generator that yields MJPEG frames for a given feed."""
    frame_interval = 1.0 / DISPLAY_FPS

    while True:
        frame = feeds[name].get_frame()
        if frame is not None:
            ret, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + buffer.tobytes()
                    + b"\r\n"
                )
        time.sleep(frame_interval)
