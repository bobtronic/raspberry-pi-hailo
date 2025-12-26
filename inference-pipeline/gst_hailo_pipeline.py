#!/usr/bin/env python3
"""
GStreamer Pipeline with Hailo-8 inference and hailotracker.

Uses identity element callback to tap detection data for Dora integration.
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import subprocess
import os
import threading
import numpy as np
from typing import Callable, Optional, List, Dict, Any

Gst.init(None)


def extract_detections_from_buffer(buffer) -> List[Dict[str, Any]]:
    """Extract detection metadata from a GStreamer buffer with HailoROI."""
    detections = []
    try:
        import hailo
        roi = hailo.get_roi_from_buffer(buffer)
        if roi is None:
            return detections

        hailo_detections = hailo.get_hailo_detections(roi)
        for det in hailo_detections:
            bbox = det.get_bbox()
            label = det.get_label()
            confidence = det.get_confidence()

            # Get track ID if available (from hailotracker)
            track_id = -1
            try:
                track_id = det.get_objects_typed(hailo.HAILO_UNIQUE_ID)
                if track_id:
                    track_id = track_id[0].get_id()
                else:
                    track_id = -1
            except Exception:
                pass

            detections.append({
                'label': label,
                'confidence': float(confidence),
                'bbox': {
                    'x': float(bbox.xmin()),
                    'y': float(bbox.ymin()),
                    'w': float(bbox.xmax() - bbox.xmin()),
                    'h': float(bbox.ymax() - bbox.ymin())
                },
                'track_id': track_id
            })
    except Exception as e:
        print(f"[Hailo] Detection extraction error: {e}")
    return detections


class GstHailoPipeline:
    """
    GStreamer pipeline for camera capture with Hailo inference and tracking.

    Pipeline:
        rpicam-vid → rawvideoparse → videoconvert → videoscale (640x640)
        → hailonet → hailofilter → hailotracker → identity (callback) → fakesink

    The identity element taps the stream for detection extraction.
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        hef_path: str = "/usr/share/hailo-models/yolov8s_h8.hef",
        postprocess_so: str = "/usr/lib/aarch64-linux-gnu/hailo/tappas/post_processes/libyolo_hailortpp_post.so",
        on_detection: Optional[Callable[[List[Dict], int, float], None]] = None,
        on_frame: Optional[Callable[[np.ndarray], None]] = None,
        enable_display: bool = False,
        enable_webrtc: bool = False
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.hef_path = hef_path
        self.postprocess_so = postprocess_so
        self.on_detection = on_detection
        self.on_frame = on_frame
        self.enable_display = enable_display
        self.enable_webrtc = enable_webrtc

        self.pipeline = None
        self.rpicam_proc = None
        self.loop = None
        self.frame_id = 0
        self._running = False

    def _on_identity_handoff(self, identity, buffer):
        """Callback for identity element - extracts detections from buffer."""
        self.frame_id += 1
        timestamp_ms = buffer.pts / 1_000_000 if buffer.pts != Gst.CLOCK_TIME_NONE else 0

        detections = extract_detections_from_buffer(buffer)

        if self.on_detection:
            try:
                self.on_detection(detections, self.frame_id, timestamp_ms)
            except Exception as e:
                print(f"[Pipeline] Detection callback error: {e}")

    def _on_webrtc_sample(self, appsink):
        """Callback for WebRTC appsink - extracts RGB frames."""
        sample = appsink.emit('pull-sample')
        if sample and self.on_frame:
            buffer = sample.get_buffer()
            caps = sample.get_caps()
            struct = caps.get_structure(0)
            width = struct.get_value('width')
            height = struct.get_value('height')

            success, map_info = buffer.map(Gst.MapFlags.READ)
            if success:
                try:
                    frame_data = np.ndarray(
                        shape=(height, width, 3),
                        dtype=np.uint8,
                        buffer=map_info.data
                    ).copy()
                    self.on_frame(frame_data)
                except Exception as e:
                    print(f"[Pipeline] Frame callback error: {e}")
                finally:
                    buffer.unmap(map_info)

        return Gst.FlowReturn.OK

    def start(self) -> bool:
        """Start the GStreamer pipeline."""
        print(f"[Pipeline] Starting rpicam-vid at {self.width}x{self.height}@{self.fps}fps...")

        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'offscreen'

        self.rpicam_proc = subprocess.Popen(
            ['rpicam-vid',
             '--width', str(self.width),
             '--height', str(self.height),
             '--framerate', str(self.fps),
             '--codec', 'yuv420',
             '-t', '0', '-n', '-o', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env
        )

        # Build pipeline string
        if self.enable_webrtc:
            # With WebRTC video output - tee at I420 level for efficiency
            webrtc_branch = f"""
                input_tee. ! queue leaky=downstream max-size-buffers=1 max-size-time=0 !
                   videoconvert ! video/x-raw,format=RGB !
                   appsink name=webrtc_sink emit-signals=true max-buffers=1 drop=true sync=false
            """
            pipeline_str = f"""
                fdsrc fd={self.rpicam_proc.stdout.fileno()} !
                rawvideoparse width={self.width} height={self.height} format=i420 framerate={self.fps}/1 !
                tee name=input_tee

                input_tee. ! queue leaky=downstream max-size-buffers=2 !
                videoconvert ! video/x-raw,format=RGB !
                videoscale ! video/x-raw,width=640,height=640 !
                queue leaky=downstream max-size-buffers=2 !
                hailonet hef-path={self.hef_path} batch-size=1 !
                queue leaky=downstream max-size-buffers=2 !
                hailofilter so-path={self.postprocess_so} function-name=filter qos=false !
                hailotracker name=tracker keep-past-metadata=true kalman-dist-thr=0.7 iou-thr=0.8 !
                identity name=dora_tap signal-handoffs=true !
                fakesink sync=false

                {webrtc_branch}
            """
        elif self.enable_display:
            # With display output
            pipeline_str = f"""
                fdsrc fd={self.rpicam_proc.stdout.fileno()} !
                rawvideoparse width={self.width} height={self.height} format=i420 framerate={self.fps}/1 !
                videoconvert ! video/x-raw,format=RGB !
                videoscale ! video/x-raw,width=640,height=640 !
                queue leaky=downstream max-size-buffers=2 !
                hailonet hef-path={self.hef_path} batch-size=1 !
                queue leaky=downstream max-size-buffers=2 !
                hailofilter so-path={self.postprocess_so} function-name=filter qos=false !
                hailotracker name=tracker keep-past-metadata=true kalman-dist-thr=0.7 iou-thr=0.8 !
                tee name=t
                    t. ! queue ! hailooverlay ! videoconvert ! autovideosink sync=false
                    t. ! queue ! identity name=dora_tap signal-handoffs=true ! fakesink sync=false
            """
        else:
            # Headless - detection extraction only
            pipeline_str = f"""
                fdsrc fd={self.rpicam_proc.stdout.fileno()} !
                rawvideoparse width={self.width} height={self.height} format=i420 framerate={self.fps}/1 !
                videoconvert ! video/x-raw,format=RGB !
                videoscale ! video/x-raw,width=640,height=640 !
                queue leaky=downstream max-size-buffers=2 !
                hailonet hef-path={self.hef_path} batch-size=1 !
                queue leaky=downstream max-size-buffers=2 !
                hailofilter so-path={self.postprocess_so} function-name=filter qos=false !
                hailotracker name=tracker keep-past-metadata=true kalman-dist-thr=0.7 iou-thr=0.8 !
                identity name=dora_tap signal-handoffs=true !
                fakesink sync=false
            """

        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            print("[Pipeline] Created successfully")
        except GLib.Error as e:
            print(f"[Pipeline] Creation error: {e}")
            return False

        # Connect identity callback
        identity = self.pipeline.get_by_name('dora_tap')
        if identity:
            identity.connect('handoff', self._on_identity_handoff)
            print("[Pipeline] Identity callback connected")
        else:
            print("[Pipeline] Warning: identity element not found")

        # Connect WebRTC appsink callback if enabled
        if self.enable_webrtc:
            webrtc_sink = self.pipeline.get_by_name('webrtc_sink')
            if webrtc_sink:
                webrtc_sink.connect('new-sample', self._on_webrtc_sample)
                print("[Pipeline] WebRTC appsink connected")
            else:
                print("[Pipeline] Warning: webrtc_sink not found")

        # Start pipeline
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("[Pipeline] Failed to start")
            return False

        self._running = True
        print("[Pipeline] Started")
        return True

    def run_loop(self):
        """Run the GLib main loop (blocking)."""
        self.loop = GLib.MainLoop()
        try:
            self.loop.run()
        except Exception:
            pass

    def run_threaded(self) -> threading.Thread:
        """Run the GLib main loop in a background thread."""
        thread = threading.Thread(target=self.run_loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        """Stop the pipeline and cleanup."""
        self._running = False

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

        if self.rpicam_proc:
            self.rpicam_proc.terminate()
            self.rpicam_proc = None

        if self.loop:
            self.loop.quit()
            self.loop = None

        print("[Pipeline] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running


# Test/demo when run directly
if __name__ == '__main__':
    import time

    def on_detection(detections, frame_id, timestamp_ms):
        if detections:
            print(f"Frame {frame_id} @ {timestamp_ms:.1f}ms: {len(detections)} detections")
            for det in detections:
                print(f"  - {det['label']} ({det['confidence']:.2f}) track={det['track_id']}")

    pipeline = GstHailoPipeline(
        width=1280,
        height=720,
        fps=30,
        on_detection=on_detection,
        enable_display=False
    )

    if pipeline.start():
        try:
            pipeline.run_loop()
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            pipeline.stop()
