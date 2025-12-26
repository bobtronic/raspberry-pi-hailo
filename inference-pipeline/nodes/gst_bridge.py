#!/usr/bin/env python3
"""
GStreamer-Dora Bridge Node

Wraps the GStreamer Hailo pipeline and publishes detection data to Dora.
Uses Apache Arrow for efficient serialization.
"""
import sys
import os
import threading
import queue
import time

# Add parent directory to path for gst_hailo_pipeline import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow as pa
from dora import Node

from gst_hailo_pipeline import GstHailoPipeline


class GstBridgeNode:
    """
    Dora node that bridges GStreamer Hailo pipeline to Dora dataflow.

    Uses a queue to safely pass detections from GStreamer thread to dora thread.

    Outputs:
        - detections: Arrow RecordBatch with detection data
        - frame: Raw frame bytes (on-demand when requested)

    Inputs:
        - frame_request: Trigger to capture next frame
    """


    # Class name to ID mapping (COCO classes)
    CLASS_MAP = {
        'person': 0, 'bicycle': 1, 'car': 2, 'motorcycle': 3, 'airplane': 4,
        'bus': 5, 'train': 6, 'truck': 7, 'boat': 8, 'traffic light': 9,
        'fire hydrant': 10, 'stop sign': 11, 'parking meter': 12, 'bench': 13,
        'bird': 14, 'cat': 15, 'dog': 16, 'horse': 17, 'sheep': 18, 'cow': 19,
        'elephant': 20, 'bear': 21, 'zebra': 22, 'giraffe': 23, 'backpack': 24,
        'umbrella': 25, 'handbag': 26, 'tie': 27, 'suitcase': 28, 'frisbee': 29,
        'skis': 30, 'snowboard': 31, 'sports ball': 32, 'kite': 33, 'baseball bat': 34,
        'baseball glove': 35, 'skateboard': 36, 'surfboard': 37, 'tennis racket': 38,
        'bottle': 39, 'wine glass': 40, 'cup': 41, 'fork': 42, 'knife': 43,
        'spoon': 44, 'bowl': 45, 'banana': 46, 'apple': 47, 'sandwich': 48,
        'orange': 49, 'broccoli': 50, 'carrot': 51, 'hot dog': 52, 'pizza': 53,
        'donut': 54, 'cake': 55, 'chair': 56, 'couch': 57, 'potted plant': 58,
        'bed': 59, 'dining table': 60, 'toilet': 61, 'tv': 62, 'laptop': 63,
        'mouse': 64, 'remote': 65, 'keyboard': 66, 'cell phone': 67, 'microwave': 68,
        'oven': 69, 'toaster': 70, 'sink': 71, 'refrigerator': 72, 'book': 73,
        'clock': 74, 'vase': 75, 'scissors': 76, 'teddy bear': 77, 'hair drier': 78,
        'toothbrush': 79
    }

    def __init__(self):
        self.node = Node()
        self.pipeline = None
        self.capture_next_frame = False
        self._detection_queue = queue.Queue(maxsize=10)
        self._running = True

        # Get config from environment
        self.width = int(os.environ.get('CAM_WIDTH', '1280'))
        self.height = int(os.environ.get('CAM_HEIGHT', '720'))
        self.fps = int(os.environ.get('CAM_FPS', '30'))
        self.enable_display = os.environ.get('ENABLE_DISPLAY', '').lower() == 'true'

    def _on_detection(self, detections, frame_id, timestamp_ms):
        """Callback from GStreamer pipeline - queues detections for dora thread."""
        try:
            # Non-blocking put - drop if queue is full
            self._detection_queue.put_nowait((detections, frame_id, timestamp_ms))
        except queue.Full:
            pass  # Drop frame if queue is full

    def _send_detections(self, detections, frame_id, timestamp_ms):
        """Send detections using zero-copy Arrow StructArray."""
        # Build list of structs for zero-copy transfer
        detection_structs = [
            {
                'frame_id': frame_id,
                'timestamp_ms': timestamp_ms,
                'track_id': d['track_id'],
                'class_id': self.CLASS_MAP.get(d['label'], 255),
                'class_name': d['label'],
                'confidence': d['confidence'],
                'x': d['bbox']['x'],
                'y': d['bbox']['y'],
                'w': d['bbox']['w'],
                'h': d['bbox']['h'],
            }
            for d in detections
        ]
        # Send as Arrow StructArray - dora handles zero-copy shared memory
        self.node.send_output("detections", pa.array(detection_structs))

    def _process_queue(self):
        """Process all queued detections and send to dora."""
        sent = 0
        while not self._detection_queue.empty():
            try:
                detections, frame_id, timestamp_ms = self._detection_queue.get_nowait()
                self._send_detections(detections, frame_id, timestamp_ms)
                sent += 1
            except queue.Empty:
                break
        return sent

    def run(self):
        """Main loop - starts pipeline and handles Dora events."""
        print(f"[GstBridge] Starting pipeline: {self.width}x{self.height}@{self.fps}fps")

        self.pipeline = GstHailoPipeline(
            width=self.width,
            height=self.height,
            fps=self.fps,
            on_detection=self._on_detection,
            enable_display=self.enable_display
        )

        if not self.pipeline.start():
            print("[GstBridge] Failed to start pipeline")
            return

        # Run GStreamer loop in background thread
        gst_thread = self.pipeline.run_threaded()
        time.sleep(1)  # Let pipeline stabilize

        print("[GstBridge] Pipeline running, processing Dora events...")

        try:
            # Process Dora events
            for event in self.node:
                event_type = event["type"]

                if event_type == "INPUT":
                    input_id = event["id"]

                    if input_id == "tick":
                        # Timer tick - process queued detections
                        self._process_queue()

                    elif input_id == "frame_request":
                        self.capture_next_frame = True

                elif event_type == "STOP":
                    print("[GstBridge] Received STOP signal")
                    break

        except KeyboardInterrupt:
            print("[GstBridge] Interrupted")
        finally:
            self._running = False
            self.pipeline.stop()
            print("[GstBridge] Shutdown complete")


def main():
    node = GstBridgeNode()
    node.run()


if __name__ == '__main__':
    main()
