from setuptools import setup, find_packages

setup(
    name="drone_direction_plugin",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "inference>=0.30",
    ],
    entry_points={
        "inference.blocks": [
            "direction_tracker_v1=drone_direction_plugin.direction_tracker.v1:DirectionOfTravelBlockV1",
        ],
    },
)
