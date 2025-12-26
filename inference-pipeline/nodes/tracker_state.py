#!/usr/bin/env python3
"""
Tracker State Node

Maintains object tracking state over time for rules engine.
Enriches raw detections with temporal history, velocity, and lifecycle events.
"""
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pyarrow as pa
from dora import Node


@dataclass
class TrackHistory:
    """History for a single tracked object."""
    track_id: int
    class_id: int
    class_name: str
    first_seen_ms: float
    last_seen_ms: float
    age_frames: int = 0
    positions: deque = field(default_factory=lambda: deque(maxlen=30))  # Last 30 positions
    confidences: deque = field(default_factory=lambda: deque(maxlen=30))
    velocity: tuple = (0.0, 0.0)  # (vx, vy) in normalized units per second
    state: str = "new"  # new, active, lost


class TrackerStateNode:
    """
    Dora node that maintains track history and enriches detection data.

    Inputs:
        - detections: Arrow RecordBatch from gst_bridge

    Outputs:
        - tracks: Enriched track data with history and velocity
    """


    def __init__(self):
        self.node = Node()
        self.tracks: Dict[int, TrackHistory] = {}

        # Config
        self.max_lost_frames = int(os.environ.get('MAX_LOST_FRAMES', '30'))
        self.velocity_window = int(os.environ.get('VELOCITY_WINDOW', '5'))

        self._last_frame_id = 0
        self._last_timestamp_ms = 0

    def _compute_velocity(self, positions: deque) -> tuple:
        """Compute velocity from recent positions."""
        if len(positions) < 2:
            return (0.0, 0.0)

        # Use last N positions for velocity calculation
        n = min(self.velocity_window, len(positions))
        recent = list(positions)[-n:]

        # Simple linear regression for velocity
        if len(recent) < 2:
            return (0.0, 0.0)

        first = recent[0]
        last = recent[-1]

        dt = (last['t'] - first['t']) / 1000.0  # Convert to seconds
        if dt <= 0:
            return (0.0, 0.0)

        # Center point velocity
        cx_first = first['x'] + first['w'] / 2
        cy_first = first['y'] + first['h'] / 2
        cx_last = last['x'] + last['w'] / 2
        cy_last = last['y'] + last['h'] / 2

        vx = (cx_last - cx_first) / dt
        vy = (cy_last - cy_first) / dt

        return (vx, vy)

    def _update_tracks(self, detections: list):
        """Update track state with new detections (list of dicts from Arrow)."""
        if not detections:
            # Mark all tracks as potentially lost
            for track in self.tracks.values():
                track.age_frames += 1
                if track.state != "lost" and track.age_frames > self.max_lost_frames:
                    track.state = "lost"
            return

        # Get frame info from first detection
        frame_id = detections[0]['frame_id']
        timestamp_ms = detections[0]['timestamp_ms']

        # Track which IDs we see this frame
        seen_ids = set()

        for det in detections:
            track_id = det['track_id']
            class_id = det['class_id']
            class_name = det['class_name']
            confidence = det['confidence']
            x = det['x']
            y = det['y']
            w = det['w']
            h = det['h']

            seen_ids.add(track_id)

            if track_id not in self.tracks:
                # New track
                self.tracks[track_id] = TrackHistory(
                    track_id=track_id,
                    class_id=class_id,
                    class_name=class_name,
                    first_seen_ms=timestamp_ms,
                    last_seen_ms=timestamp_ms,
                    state="new"
                )

            track = self.tracks[track_id]
            track.last_seen_ms = timestamp_ms
            track.age_frames += 1
            track.confidences.append(confidence)
            track.positions.append({
                'x': x, 'y': y, 'w': w, 'h': h, 't': timestamp_ms
            })

            # Update velocity
            track.velocity = self._compute_velocity(track.positions)

            # Update state
            if track.state == "new" and track.age_frames > 3:
                track.state = "active"
            elif track.state == "lost":
                track.state = "active"  # Recovered

        # Update tracks not seen this frame
        for track_id, track in self.tracks.items():
            if track_id not in seen_ids:
                # Increment lost counter
                if track.state != "lost":
                    lost_frames = (timestamp_ms - track.last_seen_ms) / (1000.0 / 30.0)
                    if lost_frames > self.max_lost_frames:
                        track.state = "lost"

        # Cleanup very old lost tracks
        to_remove = [
            tid for tid, t in self.tracks.items()
            if t.state == "lost" and (timestamp_ms - t.last_seen_ms) > 5000
        ]
        for tid in to_remove:
            del self.tracks[tid]

        self._last_frame_id = frame_id
        self._last_timestamp_ms = timestamp_ms

    def _build_output(self) -> pa.Array:
        """Build Arrow StructArray output from current track state (zero-copy)."""
        active_tracks = [
            t for t in self.tracks.values()
            if t.state in ("new", "active") and t.positions
        ]

        # Build list of structs for zero-copy transfer
        track_structs = [
            {
                'track_id': t.track_id,
                'class_id': t.class_id,
                'class_name': t.class_name,
                'confidence': t.confidences[-1] if t.confidences else 0.0,
                'x': t.positions[-1]['x'] if t.positions else 0.0,
                'y': t.positions[-1]['y'] if t.positions else 0.0,
                'w': t.positions[-1]['w'] if t.positions else 0.0,
                'h': t.positions[-1]['h'] if t.positions else 0.0,
                'age_frames': t.age_frames,
                'age_ms': t.last_seen_ms - t.first_seen_ms,
                'velocity_x': t.velocity[0],
                'velocity_y': t.velocity[1],
                'state': t.state,
            }
            for t in active_tracks
        ]

        # Return Arrow array - dora handles zero-copy shared memory
        return pa.array(track_structs)

    def run(self):
        """Main loop - process detection events and publish enriched tracks."""
        print("[TrackerState] Starting...")

        for event in self.node:
            event_type = event["type"]

            if event_type == "INPUT":
                input_id = event["id"]

                if input_id == "detections":
                    # Zero-copy Arrow - direct access to StructArray as list of dicts
                    detections = event["value"].to_pylist()

                    # Update track state
                    self._update_tracks(detections)

                    # Publish enriched tracks - zero-copy Arrow
                    output = self._build_output()
                    self.node.send_output("tracks", output)

            elif event_type == "STOP":
                print("[TrackerState] Stopping")
                break

        print("[TrackerState] Shutdown complete")


def main():
    node = TrackerStateNode()
    node.run()


if __name__ == '__main__':
    main()
