worker_class = "gevent"
workers = 1
worker_connections = 50
bind = "0.0.0.0:8000"
timeout = 0  # MJPEG streams must never time out
keepalive = 5
