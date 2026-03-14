#!/usr/bin/env python3
"""Drone Detection Security Center - Entry Point"""

from app import create_app

app = create_app()


def main():
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
