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

    # Output schema for enriched tracks
    TRACKS_SCHEMA = pa.schema([
        ('track_id', pa.int32()),
        ('class_id', pa.uint8()),
        ('class_name', pa.string()),
        ('confidence', pa.float32()),
        ('x', pa.float32()),
        ('y', pa.float32()),
        ('w', pa.float32()),
        ('h', pa.float32()),
        ('age_frames', pa.uint32()),
        ('age_ms', pa.float64()),
        ('velocity_x', pa.float32()),
        ('velocity_y', pa.float32()),
        ('state', pa.string()),
    ])

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

    def _update_tracks(self, detections: pa.RecordBatch):
        """Update track state with new detections."""
        if len(detections) == 0:
            # Mark all tracks as potentially lost
            for track in self.tracks.values():
                track.age_frames += 1
                if track.state != "lost" and track.age_frames > self.max_lost_frames:
                    track.state = "lost"
            return

        # Get frame info from first detection
        frame_id = detections['frame_id'][0].as_py()
        timestamp_ms = detections['timestamp_ms'][0].as_py()

        # Track which IDs we see this frame
        seen_ids = set()

        for i in range(len(detections)):
            track_id = detections['track_id'][i].as_py()
            class_id = detections['class_id'][i].as_py()
            class_name = detections['class_name'][i].as_py()
            confidence = detections['confidence'][i].as_py()
            x = detections['x'][i].as_py()
            y = detections['y'][i].as_py()
            w = detections['w'][i].as_py()
            h = detections['h'][i].as_py()

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

    def _build_output(self) -> bytes:
        """Build Arrow output from current track state."""
        active_tracks = [
            t for t in self.tracks.values()
            if t.state in ("new", "active") and t.positions
        ]

        if not active_tracks:
            # Empty batch
            batch = pa.RecordBatch.from_pydict({
                'track_id': pa.array([], type=pa.int32()),
                'class_id': pa.array([], type=pa.uint8()),
                'class_name': pa.array([], type=pa.string()),
                'confidence': pa.array([], type=pa.float32()),
                'x': pa.array([], type=pa.float32()),
                'y': pa.array([], type=pa.float32()),
                'w': pa.array([], type=pa.float32()),
                'h': pa.array([], type=pa.float32()),
                'age_frames': pa.array([], type=pa.uint32()),
                'age_ms': pa.array([], type=pa.float64()),
                'velocity_x': pa.array([], type=pa.float32()),
                'velocity_y': pa.array([], type=pa.float32()),
                'state': pa.array([], type=pa.string()),
            }, schema=self.TRACKS_SCHEMA)
        else:
            batch = pa.RecordBatch.from_pydict({
                'track_id': pa.array([t.track_id for t in active_tracks], type=pa.int32()),
                'class_id': pa.array([t.class_id for t in active_tracks], type=pa.uint8()),
                'class_name': pa.array([t.class_name for t in active_tracks], type=pa.string()),
                'confidence': pa.array(
                    [t.confidences[-1] if t.confidences else 0.0 for t in active_tracks],
                    type=pa.float32()
                ),
                'x': pa.array(
                    [t.positions[-1]['x'] if t.positions else 0.0 for t in active_tracks],
                    type=pa.float32()
                ),
                'y': pa.array(
                    [t.positions[-1]['y'] if t.positions else 0.0 for t in active_tracks],
                    type=pa.float32()
                ),
                'w': pa.array(
                    [t.positions[-1]['w'] if t.positions else 0.0 for t in active_tracks],
                    type=pa.float32()
                ),
                'h': pa.array(
                    [t.positions[-1]['h'] if t.positions else 0.0 for t in active_tracks],
                    type=pa.float32()
                ),
                'age_frames': pa.array([t.age_frames for t in active_tracks], type=pa.uint32()),
                'age_ms': pa.array(
                    [t.last_seen_ms - t.first_seen_ms for t in active_tracks],
                    type=pa.float64()
                ),
                'velocity_x': pa.array([t.velocity[0] for t in active_tracks], type=pa.float32()),
                'velocity_y': pa.array([t.velocity[1] for t in active_tracks], type=pa.float32()),
                'state': pa.array([t.state for t in active_tracks], type=pa.string()),
            }, schema=self.TRACKS_SCHEMA)

        # Serialize
        sink = pa.BufferOutputStream()
        writer = pa.ipc.new_stream(sink, self.TRACKS_SCHEMA)
        writer.write_batch(batch)
        writer.close()

        return sink.getvalue().to_pybytes()

    def run(self):
        """Main loop - process detection events and publish enriched tracks."""
        print("[TrackerState] Starting...")

        for event in self.node:
            event_type = event["type"]

            if event_type == "INPUT":
                input_id = event["id"]

                if input_id == "detections":
                    # Deserialize Arrow data - dora returns UInt8Array, convert to bytes
                    data = event["value"]
                    if hasattr(data, 'to_pylist'):
                        data = bytes(data.to_pylist())
                    elif hasattr(data, 'to_pybytes'):
                        data = data.to_pybytes()
                    reader = pa.ipc.open_stream(data)
                    batch = reader.read_next_batch()

                    # Update track state
                    self._update_tracks(batch)

                    # Publish enriched tracks
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
