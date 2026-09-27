"""
In-memory hub simulation for Step 5.
Stores snapshots of the mutable shard (as list of points) that can be pulled by edge nodes.
"""

from typing import Dict, Any, List, Optional
from qdrant_edge import Point

class InMemoryHub:
    def __init__(self):
        self.snapshots: Dict[str, List[Point]] = {}  # version -> list of points
        self.latest_version: Optional[str] = None

    def create_snapshot(self, points: List[Point]) -> str:
        """
        Create a snapshot of the given points.
        Returns a version string.
        """
        version = f"v{len(self.snapshots)+1}"
        self.snapshots[version] = list(points)  # shallow copy
        self.latest_version = version
        return version

    def get_snapshot(self, version: str) -> Optional[List[Point]]:
        return self.snapshots.get(version)

    def get_latest_snapshot(self) -> Optional[List[Point]]:
        if self.latest_version:
            return self.snapshots.get(self.latest_version)
        return None

# Global hub instance
hub = InMemoryHub()