#!/usr/bin/env python3
"""
WebRTC + HLS Hybrid Server for Hailo Detection

- Live: WebRTC for ultra-low latency (~100-200ms)
- Recording: HLS segments + JSON sidecar for playback
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import asyncio
import json
import threading
import subprocess
import time
import os
import shutil
import math
import fractions
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
import numpy as np

# Event loop reference for cross-thread signaling
main_loop = None

Gst.init(None)

HTTP_PORT = 8080
HLS_DIR = "/tmp/hls"
RECORDINGS_DIR = "/home/bob/recordings"
EVENTS_LOG = "/home/bob/recordings/events.jsonl"

# Tracking parameters
IOU_THRESHOLD = 0.3
MIN_HITS = 8
MAX_AGE = 15
MIN_CONFIDENCE = 0.4
EURO_MIN_CUTOFF = 0.5
EURO_BETA = 0.02


class OneEuroFilter:
    def __init__(self, min_cutoff=1.0, beta=0.007, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x_prev = None
        self.dx_prev = 0
        self.t_prev = None

    def filter(self, x, t):
        if self.t_prev is None:
            self.x_prev = x
            self.t_prev = t
            return x
        dt = t - self.t_prev
        if dt <= 0:
            return self.x_prev
        dx = (x - self.x_prev) / dt
        tau_d = 1.0 / (2 * math.pi * self.d_cutoff)
        alpha_d = 1.0 / (1.0 + tau_d / dt)
        dx_smooth = alpha_d * dx + (1 - alpha_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_smooth)
        tau = 1.0 / (2 * math.pi * cutoff)
        alpha = 1.0 / (1.0 + tau / dt)
        x_smooth = alpha * x + (1 - alpha) * self.x_prev
        self.x_prev = x_smooth
        self.dx_prev = dx_smooth
        self.t_prev = t
        return x_smooth


class TrackedObject:
    def __init__(self, label, bbox, confidence, timestamp):
        self.label = label
        self.bbox = dict(bbox)
        self.confidence = confidence
        self.hits = 1
        self.age = 0
        self.first_seen = timestamp
        self.last_seen = timestamp
        self.velocity = (0.0, 0.0)
        self.prev_center = (bbox['x'] + bbox['w']/2, bbox['y'] + bbox['h']/2)
        self.prev_time = timestamp
        self.filters = {
            'x': OneEuroFilter(EURO_MIN_CUTOFF, EURO_BETA),
            'y': OneEuroFilter(EURO_MIN_CUTOFF, EURO_BETA),
            'w': OneEuroFilter(EURO_MIN_CUTOFF, EURO_BETA),
            'h': OneEuroFilter(EURO_MIN_CUTOFF, EURO_BETA),
            'conf': OneEuroFilter(0.3, 0.01)
        }

    def update(self, bbox, confidence, timestamp):
        self.bbox = {
            'x': self.filters['x'].filter(bbox['x'], timestamp),
            'y': self.filters['y'].filter(bbox['y'], timestamp),
            'w': self.filters['w'].filter(bbox['w'], timestamp),
            'h': self.filters['h'].filter(bbox['h'], timestamp)
        }
        self.confidence = self.filters['conf'].filter(confidence, timestamp)
        self.hits += 1
        self.age = 0
        self.last_seen = timestamp

        # Calculate velocity
        cx = self.bbox['x'] + self.bbox['w'] / 2
        cy = self.bbox['y'] + self.bbox['h'] / 2
        dt = timestamp - self.prev_time
        if dt > 0:
            vx = (cx - self.prev_center[0]) / dt
            vy = (cy - self.prev_center[1]) / dt
            # Smooth velocity
            self.velocity = (0.7 * self.velocity[0] + 0.3 * vx,
                           0.7 * self.velocity[1] + 0.3 * vy)
        self.prev_center = (cx, cy)
        self.prev_time = timestamp

    @property
    def age_seconds(self):
        return self.last_seen - self.first_seen

    @property
    def speed(self):
        return math.sqrt(self.velocity[0]**2 + self.velocity[1]**2)


def compute_iou(a, b):
    x1 = max(a['x'], b['x'])
    y1 = max(a['y'], b['y'])
    x2 = min(a['x'] + a['w'], b['x'] + b['w'])
    y2 = min(a['y'] + a['h'], b['y'] + b['h'])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = a['w'] * a['h']
    area_b = b['w'] * b['h']
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0


class DetectionTracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 0

    def update(self, raw_detections, timestamp):
        filtered = [d for d in raw_detections if d['confidence'] >= MIN_CONFIDENCE]
        matched = set()

        for det in filtered:
            best_track_id = None
            best_iou = IOU_THRESHOLD

            for track_id, track in self.tracks.items():
                if track.label != det['label'] or track_id in matched:
                    continue
                iou = compute_iou(track.bbox, det['bbox'])
                if iou > best_iou:
                    best_iou = iou
                    best_track_id = track_id

            if best_track_id is not None:
                self.tracks[best_track_id].update(det['bbox'], det['confidence'], timestamp)
                matched.add(best_track_id)
            else:
                self.tracks[self.next_id] = TrackedObject(
                    det['label'], det['bbox'], det['confidence'], timestamp
                )
                matched.add(self.next_id)
                self.next_id += 1

        for track_id in self.tracks:
            if track_id not in matched:
                self.tracks[track_id].age += 1

        to_delete = [tid for tid, t in self.tracks.items() if t.age > MAX_AGE * 2]
        for tid in to_delete:
            del self.tracks[tid]

        output = []
        for track_id, track in self.tracks.items():
            if track.hits >= MIN_HITS and track.age <= MAX_AGE:
                output.append({
                    'label': track.label,
                    'confidence': round(track.confidence, 3),
                    'bbox': {k: round(v, 6) for k, v in track.bbox.items()},
                    'track_id': track_id
                })
        return output


class RulesEngine:
    """Simple rules engine for detection analytics."""

    # Define zones as polygons (normalized 0-1 coordinates)
    ZONES = {
        'left': [(0, 0), (0.33, 0), (0.33, 1), (0, 1)],
        'center': [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)],
        'right': [(0.67, 0), (1, 0), (1, 1), (0.67, 1)],
    }

    def __init__(self):
        self.events = []
        self.max_events = 100
        self.cooldowns = {}  # rule_key -> last_trigger_time
        self.zone_entries = {}  # track_id -> {zone: entry_time}

    def _point_in_polygon(self, x, y, polygon):
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _emit_event(self, event_type, track_id, label, message, data=None):
        event = {
            'timestamp': time.time(),
            'type': event_type,
            'track_id': track_id,
            'label': label,
            'message': message,
            'data': data or {}
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events.pop(0)

        # Log to file
        try:
            os.makedirs(os.path.dirname(EVENTS_LOG), exist_ok=True)
            with open(EVENTS_LOG, 'a') as f:
                f.write(json.dumps(event) + '\n')
        except Exception:
            pass

        print(f"[Event] {message}")
        return event

    def _check_cooldown(self, key, cooldown_sec=5.0):
        now = time.time()
        if key in self.cooldowns:
            if now - self.cooldowns[key] < cooldown_sec:
                return False
        self.cooldowns[key] = now
        return True

    def evaluate(self, tracker):
        """Evaluate rules against current tracker state."""
        current_time = time.time()
        new_events = []

        for track_id, track in tracker.tracks.items():
            if track.hits < MIN_HITS or track.age > MAX_AGE:
                continue

            cx = track.bbox['x'] + track.bbox['w'] / 2
            cy = track.bbox['y'] + track.bbox['h'] / 2

            # Initialize zone tracking for this track
            if track_id not in self.zone_entries:
                self.zone_entries[track_id] = {}

            # Zone presence rules
            for zone_name, polygon in self.ZONES.items():
                in_zone = self._point_in_polygon(cx, cy, polygon)
                zone_times = self.zone_entries[track_id]

                if in_zone:
                    if zone_name not in zone_times:
                        zone_times[zone_name] = current_time

                    duration = current_time - zone_times[zone_name]
                    if duration >= 2.0:  # 2 second threshold
                        key = f"zone_{zone_name}_{track_id}"
                        if self._check_cooldown(key, 10.0):
                            evt = self._emit_event(
                                'zone_presence', track_id, track.label,
                                f"{track.label} in {zone_name} zone for {duration:.1f}s",
                                {'zone': zone_name, 'duration': duration}
                            )
                            new_events.append(evt)
                else:
                    if zone_name in zone_times:
                        del zone_times[zone_name]

            # Fast motion detection
            if track.speed > 0.3:
                key = f"fast_motion_{track_id}"
                if self._check_cooldown(key, 3.0):
                    evt = self._emit_event(
                        'fast_motion', track_id, track.label,
                        f"Fast moving {track.label} (speed: {track.speed:.2f})",
                        {'speed': track.speed, 'velocity': track.velocity}
                    )
                    new_events.append(evt)

            # Stationary detection (low speed for extended time)
            if track.speed < 0.01 and track.age_seconds > 10:
                key = f"stationary_{track_id}"
                if self._check_cooldown(key, 30.0):
                    evt = self._emit_event(
                        'stationary', track_id, track.label,
                        f"Stationary {track.label} for {track.age_seconds:.1f}s",
                        {'duration': track.age_seconds, 'position': (cx, cy)}
                    )
                    new_events.append(evt)

            # Persistence detection (tracked for extended time)
            if track.age_seconds >= 5.0:
                key = f"persistent_{track_id}"
                if self._check_cooldown(key, 30.0):
                    evt = self._emit_event(
                        'persistent', track_id, track.label,
                        f"{track.label} tracked for {track.age_seconds:.1f}s",
                        {'duration': track.age_seconds}
                    )
                    new_events.append(evt)

        # Cleanup old zone entries for tracks that no longer exist
        active_ids = set(tracker.tracks.keys())
        for tid in list(self.zone_entries.keys()):
            if tid not in active_ids:
                del self.zone_entries[tid]

        return new_events

    def get_recent_events(self, limit=20):
        return self.events[-limit:]


# Global state
detection_tracker = DetectionTracker()
rules_engine = RulesEngine()
latest_detections = {"detections": [], "fps": 0, "timestamp": 0}
detection_lock = threading.Lock()

is_recording = False
recording_path = None
recording_start_time = None
recording_vtt_file = None

frame_count = 0
start_time = time.time()

# WebRTC peers
pcs = set()


# Frame queue for WebRTC
frame_queue = asyncio.Queue(maxsize=2)
latest_frame = None
frame_lock = threading.Lock()


class HailoVideoTrack(VideoStreamTrack):
    """Video track that serves frames from the pipeline."""

    kind = "video"

    def __init__(self):
        super().__init__()
        self._start = time.time()

    async def recv(self):
        try:
            frame_data = await asyncio.wait_for(frame_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            with frame_lock:
                frame_data = latest_frame

        if frame_data is not None:
            frame = VideoFrame.from_ndarray(frame_data, format="rgb24")
        else:
            frame = VideoFrame.from_ndarray(
                np.zeros((720, 1280, 3), dtype=np.uint8), format="rgb24"
            )

        elapsed = time.time() - self._start
        frame.pts = int(elapsed * 90000)
        frame.time_base = fractions.Fraction(1, 90000)
        return frame


def extract_detections_from_buffer(buffer):
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
            detections.append({
                'label': label,
                'confidence': float(confidence),
                'bbox': {
                    'x': float(bbox.xmin()),
                    'y': float(bbox.ymin()),
                    'w': float(bbox.xmax() - bbox.xmin()),
                    'h': float(bbox.ymax() - bbox.ymin())
                }
            })
    except Exception:
        pass
    return detections


def on_detection_sample(appsink):
    """Callback for inference branch - extracts detections only."""
    global frame_count, latest_detections, start_time

    sample = appsink.emit('pull-sample')
    if sample:
        buffer = sample.get_buffer()
        raw_detections = extract_detections_from_buffer(buffer)

        frame_count += 1
        elapsed = time.time() - start_time
        fps = frame_count / elapsed if elapsed > 0 else 0

        # Apply tracking
        smoothed = detection_tracker.update(raw_detections, elapsed)

        # Evaluate rules
        new_events = rules_engine.evaluate(detection_tracker)

        with detection_lock:
            latest_detections = {
                "detections": smoothed,
                "fps": round(fps, 1),
                "timestamp": round(elapsed, 3),
                "events": [e['message'] for e in new_events[-5:]]  # Last 5 new events
            }

            if is_recording and recording_start_time:
                rec_time = time.time() - recording_start_time
                write_vtt_cue(rec_time, smoothed)

    return Gst.FlowReturn.OK


def on_video_sample(appsink):
    """Callback for video branch - RGB frames for WebRTC."""
    global latest_frame

    sample = appsink.emit('pull-sample')
    if sample:
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        width = struct.get_value('width')
        height = struct.get_value('height')

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if success:
            frame_data = np.ndarray(
                shape=(height, width, 3),
                dtype=np.uint8,
                buffer=map_info.data
            ).copy()
            buffer.unmap(map_info)

            with frame_lock:
                latest_frame = frame_data

            if main_loop is not None:
                try:
                    main_loop.call_soon_threadsafe(
                        lambda: frame_queue.put_nowait(frame_data) if not frame_queue.full() else None
                    )
                except:
                    pass

    return Gst.FlowReturn.OK


class GStreamerPipeline:
    def __init__(self):
        self.pipeline = None
        self.rpicam_proc = None
        self.loop = None
        self.width = 1280
        self.height = 720

    def start(self):
        print("[Camera] Starting rpicam-vid...")

        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'offscreen'

        self.width = int(os.environ.get('CAM_WIDTH', '1280'))
        self.height = int(os.environ.get('CAM_HEIGHT', '720'))
        self.fps = int(os.environ.get('CAM_FPS', '30'))

        print(f"[Camera] Resolution: {self.width}x{self.height} @ {self.fps}fps")

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

        hef_path = "/usr/share/hailo-models/yolov8s_h8.hef"
        postprocess_so = "/usr/lib/aarch64-linux-gnu/hailo/tappas/post_processes/libyolo_hailortpp_post.so"

        # Optimized pipeline: tee I420 early
        # - Inference branch: I420 → RGB → 640x640 → hailo (detections only)
        # - Video branch: I420 → RGB (WebRTC) and I420 → H.264 (HLS)
        pipeline_str = f"""
            fdsrc fd={self.rpicam_proc.stdout.fileno()} !
            rawvideoparse width={self.width} height={self.height} format=i420 framerate={self.fps}/1 !
            tee name=input_tee

            input_tee. ! queue leaky=downstream max-size-buffers=1 max-size-time=0 !
               videoconvert ! video/x-raw,format=RGB !
               videoscale ! video/x-raw,width=640,height=640 !
               queue leaky=downstream max-size-buffers=1 max-size-time=0 !
               hailonet hef-path={hef_path} batch-size=1 !
               queue leaky=downstream max-size-buffers=1 max-size-time=0 !
               hailofilter so-path={postprocess_so} function-name=filter qos=false !
               appsink name=detection_sink emit-signals=true max-buffers=1 drop=true sync=false

            input_tee. ! queue leaky=downstream max-size-buffers=1 max-size-time=0 !
               videoconvert ! video/x-raw,format=RGB !
               appsink name=webrtc_sink emit-signals=true max-buffers=1 drop=true sync=false

            input_tee. ! queue leaky=downstream max-size-buffers=2 !
               openh264enc bitrate=2500000 complexity=low !
               h264parse config-interval=1 !
               hlssink2 name=hls_sink
                        location={HLS_DIR}/segment%05d.ts
                        playlist-location={HLS_DIR}/playlist.m3u8
                        target-duration=1 max-files=10 playlist-length=5
        """

        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            print("[GStreamer] Pipeline created")
        except GLib.Error as e:
            print(f"[GStreamer] Pipeline error: {e}")
            return False

        # Connect detection appsink (inference branch - extracts detections only)
        detection_sink = self.pipeline.get_by_name('detection_sink')
        if detection_sink:
            detection_sink.connect('new-sample', on_detection_sample)
            print("[GStreamer] Detection appsink connected")

        # Connect video appsink (RGB for WebRTC)
        video_sink = self.pipeline.get_by_name('webrtc_sink')
        if video_sink:
            video_sink.connect('new-sample', on_video_sample)
            print("[GStreamer] WebRTC appsink connected")

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("[GStreamer] Failed to start")
            return False

        print("[GStreamer] Pipeline started")
        return True

    def run_loop(self):
        self.loop = GLib.MainLoop()
        try:
            self.loop.run()
        except:
            pass

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.rpicam_proc:
            self.rpicam_proc.terminate()
        if self.loop:
            self.loop.quit()


# Recording functions

def format_vtt_time(seconds):
    """Format seconds as VTT timestamp: HH:MM:SS.mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def write_vtt_cue(timestamp, detections):
    """Write a detection frame as a VTT cue."""
    global recording_vtt_file
    if recording_vtt_file is None:
        return

    start_time = timestamp
    end_time = timestamp + 0.05  # 50ms duration

    try:
        recording_vtt_file.write(f"\n{format_vtt_time(start_time)} --> {format_vtt_time(end_time)}\n")
        recording_vtt_file.write(f"{json.dumps(detections)}\n")
        recording_vtt_file.flush()
    except Exception as e:
        print(f"[VTT] Write error: {e}")


def start_recording():
    global is_recording, recording_path, recording_start_time, recording_vtt_file
    from datetime import datetime
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    recording_path = os.path.join(RECORDINGS_DIR, timestamp)
    os.makedirs(recording_path, exist_ok=True)
    recording_start_time = time.time()

    # Create VTT file for detection metadata
    vtt_path = os.path.join(recording_path, "detections.vtt")
    recording_vtt_file = open(vtt_path, "w")
    recording_vtt_file.write("WEBVTT\n")
    recording_vtt_file.write("Kind: metadata\n")
    recording_vtt_file.write("Language: en\n")
    recording_vtt_file.flush()

    is_recording = True

    # Start watching for new segments
    import glob
    existing = set(glob.glob(os.path.join(HLS_DIR, "*.ts")))
    threading.Thread(target=watch_segments, args=(existing,), daemon=True).start()

    print(f"[Recording] Started: {recording_path}")
    return recording_path


def watch_segments(existing_before):
    """Watch for new HLS segments and copy them during recording."""
    import glob
    segment_list = []

    while is_recording:
        current = set(glob.glob(os.path.join(HLS_DIR, "*.ts")))
        new_segs = current - existing_before
        for seg in sorted(new_segs):
            if seg not in segment_list:
                segment_list.append(seg)
                # Copy immediately to recording folder with sequential numbering
                if recording_path:
                    idx = len(segment_list) - 1
                    dst = os.path.join(recording_path, f"segment{idx:05d}.ts")
                    try:
                        shutil.copy(seg, dst)
                    except:
                        pass
        existing_before = current
        time.sleep(0.5)


def stop_recording():
    global is_recording, recording_path, recording_vtt_file
    if not is_recording:
        return None
    is_recording = False
    time.sleep(1)  # Let final segments be copied

    # Close VTT file
    if recording_vtt_file:
        try:
            recording_vtt_file.close()
        except:
            pass
        recording_vtt_file = None

    import glob
    ts_files = sorted(glob.glob(os.path.join(recording_path, "*.ts")))
    if ts_files:
        playlist = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        for ts in ts_files:
            playlist += f"#EXTINF:1.0,\n{os.path.basename(ts)}\n"
        playlist += "#EXT-X-ENDLIST\n"
        with open(os.path.join(recording_path, "playlist.m3u8"), "w") as f:
            f.write(playlist)

    saved_path = recording_path
    recording_path = None
    print(f"[Recording] Saved: {saved_path}")
    return saved_path


# Web handlers
async def index(request):
    html = '''<!DOCTYPE html>
<html>
<head>
    <title>Hailo WebRTC Detection</title>
    <style>
        body { margin: 0; padding: 20px; background: #1a1a1a; color: #fff; font-family: Arial; text-align: center; }
        h1 { color: #00ff88; margin-bottom: 5px; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 20px; }
        .main-layout { display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; }
        #container { position: relative; display: inline-block; }
        #video { max-width: 1280px; width: 100%; display: block; border: 3px solid #333; border-radius: 8px; background: #000; }
        #canvas { position: absolute; top: 3px; left: 3px; pointer-events: none; }
        .info { margin-top: 10px; padding: 15px; background: #2a2a2a; border-radius: 8px; text-align: left; }
        .label { color: #00ff88; font-weight: bold; display: inline-block; width: 100px; }
        .status-ok { color: #00ff88; }
        button { font-size: 12px; padding: 4px 12px; border: none; border-radius: 4px; cursor: pointer; margin: 2px; }
        .rec-btn { background: #c00; color: #fff; }
        .rec-btn.recording { background: #090; }
        .connect-btn { background: #0066cc; color: #fff; }
        .events-panel { width: 300px; background: #2a2a2a; border-radius: 8px; padding: 15px; text-align: left; max-height: 600px; overflow-y: auto; }
        .events-panel h3 { color: #00ff88; margin: 0 0 10px 0; font-size: 14px; }
        .event-item { padding: 8px; margin: 4px 0; background: #333; border-radius: 4px; font-size: 12px; border-left: 3px solid #666; }
        .event-item.zone_presence { border-left-color: #4ecdc4; }
        .event-item.fast_motion { border-left-color: #ff6b6b; }
        .event-item.stationary { border-left-color: #ffe66d; }
        .event-item.persistent { border-left-color: #95e1d3; }
        .event-time { color: #666; font-size: 10px; }
        .event-type { color: #888; font-size: 10px; text-transform: uppercase; }
    </style>
</head>
<body>
    <h1>Hailo-8 WebRTC Detection</h1>
    <div class="subtitle">Ultra-low latency WebRTC streaming with rules engine</div>
    <div class="main-layout">
        <div id="container">
            <video id="video" autoplay muted playsinline></video>
            <canvas id="canvas"></canvas>
            <div class="info">
                <div><span class="label">Status:</span> <span id="status">Connecting...</span></div>
                <div><span class="label">Latency:</span> <span id="latency">--</span> ms</div>
                <div><span class="label">FPS:</span> <span id="fps">0</span></div>
                <div><span class="label">Objects:</span> <span id="count">0</span></div>
                <div>
                    <span class="label">Controls:</span>
                    <button class="connect-btn" id="connectBtn" onclick="connect()">Connect</button>
                    <button class="rec-btn" id="recBtn" onclick="toggleRecording()">● REC</button>
                    <span id="recStatus"></span>
                </div>
                <div><a href="/playback" style="color:#4ecdc4;">View Recordings</a></div>
            </div>
        </div>
        <div class="events-panel">
            <h3>Live Events</h3>
            <div id="events">Waiting for events...</div>
        </div>
    </div>

    <script>
        const video = document.getElementById('video');
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d');
        const statusEl = document.getElementById('status');
        const latencyEl = document.getElementById('latency');
        const fpsEl = document.getElementById('fps');
        const countEl = document.getElementById('count');

        let pc = null;
        let ws = null;
        let isRecording = false;
        let allEvents = [];
        const eventsEl = document.getElementById('events');

        const colors = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#95e1d3', '#f38181', '#aa96da'];
        const eventColors = {
            'zone_presence': '#4ecdc4',
            'fast_motion': '#ff6b6b',
            'stationary': '#ffe66d',
            'persistent': '#95e1d3'
        };
        function getColor(label) {
            let hash = 0;
            for (let i = 0; i < label.length; i++) hash = label.charCodeAt(i) + ((hash << 5) - hash);
            return colors[Math.abs(hash) % colors.length];
        }

        function resizeCanvas() {
            const rect = video.getBoundingClientRect();
            canvas.width = rect.width;
            canvas.height = rect.height;
        }
        video.addEventListener('loadedmetadata', resizeCanvas);
        window.addEventListener('resize', resizeCanvas);

        async function connect() {
            statusEl.textContent = 'Connecting...';
            document.getElementById('connectBtn').disabled = true;

            pc = new RTCPeerConnection({
                iceServers: [{urls: 'stun:stun.l.google.com:19302'}]
            });

            pc.ontrack = (event) => {
                video.srcObject = event.streams[0];
                statusEl.textContent = 'Connected';
                statusEl.className = 'status-ok';
                resizeCanvas();
            };

            pc.oniceconnectionstatechange = () => {
                if (pc.iceConnectionState === 'disconnected' || pc.iceConnectionState === 'failed') {
                    statusEl.textContent = 'Disconnected';
                    document.getElementById('connectBtn').disabled = false;
                }
            };

            pc.addTransceiver('video', {direction: 'recvonly'});

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            // Wait for ICE gathering
            await new Promise(resolve => {
                if (pc.iceGatheringState === 'complete') resolve();
                else pc.onicegatheringstatechange = () => {
                    if (pc.iceGatheringState === 'complete') resolve();
                };
            });

            const response = await fetch('/offer', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: pc.localDescription.type
                })
            });

            const answer = await response.json();
            await pc.setRemoteDescription(new RTCSessionDescription(answer));

            // Start detection WebSocket
            connectWS();
        }

        function formatEventTime(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString();
        }

        function renderEvents() {
            if (allEvents.length === 0) {
                eventsEl.innerHTML = '<div style="color:#666">No events yet...</div>';
                return;
            }
            eventsEl.innerHTML = allEvents.slice(-20).reverse().map(e => `
                <div class="event-item ${e.type}">
                    <div class="event-time">${formatEventTime(e.timestamp)}</div>
                    <div>${e.message}</div>
                    <div class="event-type">${e.type}</div>
                </div>
            `).join('');
        }

        // Load historical events on startup
        fetch('/api/events?limit=20').then(r => r.json()).then(events => {
            allEvents = events;
            renderEvents();
        }).catch(() => {});

        function connectWS() {
            ws = new WebSocket(`ws://${window.location.hostname}:${window.location.port}/ws`);
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                fpsEl.textContent = data.fps || 0;

                const dets = data.detections || [];
                countEl.textContent = dets.length;

                // Handle new events
                if (data.events && data.events.length > 0) {
                    data.events.forEach(msg => {
                        // Create event object from message
                        allEvents.push({
                            timestamp: Date.now() / 1000,
                            type: msg.includes('zone') ? 'zone_presence' :
                                  msg.includes('Fast') ? 'fast_motion' :
                                  msg.includes('Stationary') ? 'stationary' : 'persistent',
                            message: msg
                        });
                    });
                    // Keep last 100 events
                    if (allEvents.length > 100) allEvents = allEvents.slice(-100);
                    renderEvents();
                }

                ctx.clearRect(0, 0, canvas.width, canvas.height);
                const scaleX = canvas.width;
                const scaleY = canvas.height;

                dets.forEach(det => {
                    const x = det.bbox.x * scaleX;
                    const y = det.bbox.y * scaleY;
                    const w = det.bbox.w * scaleX;
                    const h = det.bbox.h * scaleY;
                    const color = getColor(det.label);

                    ctx.strokeStyle = color;
                    ctx.lineWidth = 3;
                    ctx.strokeRect(x, y, w, h);

                    const label = `${det.label} ${(det.confidence*100).toFixed(0)}%`;
                    ctx.font = 'bold 14px Arial';
                    const textWidth = ctx.measureText(label).width;
                    ctx.fillStyle = color;
                    ctx.fillRect(x, y - 22, textWidth + 10, 22);
                    ctx.fillStyle = '#000';
                    ctx.fillText(label, x + 5, y - 6);
                });
            };
            ws.onclose = () => setTimeout(connectWS, 1000);
        }

        async function toggleRecording() {
            const btn = document.getElementById('recBtn');
            const status = document.getElementById('recStatus');

            if (!isRecording) {
                await fetch('/start-recording');
                isRecording = true;
                btn.textContent = '■ STOP';
                btn.classList.add('recording');
                let sec = 0;
                window.recInterval = setInterval(() => {
                    sec++;
                    status.textContent = `${Math.floor(sec/60)}:${(sec%60).toString().padStart(2,'0')}`;
                }, 1000);
            } else {
                const resp = await fetch('/stop-recording');
                const data = await resp.json();
                isRecording = false;
                btn.textContent = '● REC';
                btn.classList.remove('recording');
                clearInterval(window.recInterval);
                status.textContent = 'Saved!';
            }
        }

        // Auto-connect on load
        connect();
    </script>
</body>
</html>'''
    return web.Response(text=html, content_type='text/html')


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params['sdp'], type=params['type'])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on('connectionstatechange')
    async def on_connectionstatechange():
        if pc.connectionState == 'failed':
            await pc.close()
            pcs.discard(pc)

    # Add video track
    video_track = HailoVideoTrack()
    pc.addTrack(video_track)

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        'sdp': pc.localDescription.sdp,
        'type': pc.localDescription.type
    })


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        while True:
            with detection_lock:
                data = latest_detections.copy()
            await ws.send_json(data)
            await asyncio.sleep(0.033)
    except:
        pass

    return ws


async def start_rec(request):
    path = start_recording()
    return web.json_response({"status": "recording", "path": path})


async def stop_rec(request):
    path = stop_recording()
    return web.json_response({"status": "stopped", "path": path})


async def playback_list(request):
    html = '''<!DOCTYPE html>
<html>
<head><title>Recordings</title>
<style>
    body { margin: 0; padding: 20px; background: #1a1a1a; color: #fff; font-family: Arial; }
    h1 { color: #00ff88; }
    a { color: #4ecdc4; text-decoration: none; }
    .rec { padding: 10px; margin: 5px 0; background: #2a2a2a; border-radius: 4px; }
</style>
</head>
<body>
    <h1>Recordings</h1>
    <p><a href="/">← Back to Live</a></p>
    <div id="list">Loading...</div>
    <script>
        fetch('/api/recordings').then(r => r.json()).then(recs => {
            document.getElementById('list').innerHTML = recs.length === 0 ? '<p>No recordings</p>' :
                recs.map(r => `<div class="rec"><a href="/playback/${r.name}">${r.name}</a> (${r.duration}s)</div>`).join('');
        });
    </script>
</body>
</html>'''
    return web.Response(text=html, content_type='text/html')


async def api_recordings(request):
    recordings = []
    if os.path.exists(RECORDINGS_DIR):
        for name in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
            rec_path = os.path.join(RECORDINGS_DIR, name)
            if os.path.isdir(rec_path):
                # Estimate duration from segment count (1 sec per segment)
                import glob
                segments = glob.glob(os.path.join(rec_path, "*.ts"))
                duration = len(segments)
                recordings.append({"name": name, "duration": duration})
    return web.json_response(recordings)


async def api_events(request):
    """Return recent events."""
    limit = int(request.query.get('limit', 50))
    events = rules_engine.get_recent_events(limit)
    return web.json_response(events)


async def playback_page(request):
    name = request.match_info['name']
    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>Playback: {name}</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <style>
        body {{ margin: 0; padding: 20px; background: #1a1a1a; color: #fff; font-family: Arial; text-align: center; }}
        h1 {{ color: #00ff88; }}
        a {{ color: #4ecdc4; }}
        #container {{ position: relative; display: inline-block; }}
        #video {{ max-width: 1280px; width: 100%; border: 3px solid #333; border-radius: 8px; }}
        #canvas {{ position: absolute; top: 3px; left: 3px; pointer-events: none; }}
        .info {{ margin-top: 10px; padding: 15px; background: #2a2a2a; border-radius: 8px; }}
        .label {{ color: #00ff88; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>Playback: {name}</h1>
    <p><a href="/playback">← Back</a></p>
    <div id="container">
        <video id="video" controls crossorigin="anonymous">
            <track id="detectionTrack" kind="metadata" src="/recordings/{name}/detections.vtt" default>
        </video>
        <canvas id="canvas"></canvas>
        <div class="info">
            <span class="label">Time:</span> <span id="time">0.0</span>s |
            <span class="label">Objects:</span> <span id="count">0</span> |
            <span class="label">Sync offset:</span>
            <input type="range" id="offsetSlider" min="-3" max="3" step="0.1" value="0" style="width:100px;vertical-align:middle;">
            <span id="offsetVal">0.0</span>s
        </div>
    </div>
    <script>
        const video = document.getElementById('video');
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d');
        let allCues = [];

        // Adjustable sync offset (negative = boxes earlier, positive = boxes later)
        let vttOffset = 0;
        document.getElementById('offsetSlider').oninput = (e) => {{
            vttOffset = parseFloat(e.target.value);
            document.getElementById('offsetVal').textContent = vttOffset.toFixed(1);
        }};

        if (Hls.isSupported()) {{
            const hls = new Hls();
            hls.loadSource('/recordings/{name}/playlist.m3u8');
            hls.attachMedia(video);
        }} else {{
            video.src = '/recordings/{name}/playlist.m3u8';
        }}

        function resizeCanvas() {{
            const rect = video.getBoundingClientRect();
            canvas.width = rect.width;
            canvas.height = rect.height;
        }}
        video.addEventListener('loadedmetadata', resizeCanvas);
        window.addEventListener('resize', resizeCanvas);

        // Load VTT and cache all cues for offset-based lookup
        fetch('/recordings/{name}/detections.vtt')
            .then(r => r.text())
            .then(vtt => {{
                const lines = vtt.split('\\n');
                let i = 0;
                while (i < lines.length) {{
                    const line = lines[i];
                    if (line.includes('-->')) {{
                        const [start] = line.split('-->').map(t => {{
                            const parts = t.trim().split(':');
                            return parseFloat(parts[0])*3600 + parseFloat(parts[1])*60 + parseFloat(parts[2]);
                        }});
                        const data = lines[i+1];
                        if (data && data.startsWith('[')) {{
                            try {{ allCues.push({{ time: start, dets: JSON.parse(data) }}); }} catch(e) {{}}
                        }}
                    }}
                    i++;
                }}
                console.log('Loaded', allCues.length, 'detection cues');
            }});

        function findDetections(videoTime) {{
            // Look for cue matching video time + offset
            const targetTime = videoTime + vttOffset;
            let best = null;
            for (const cue of allCues) {{
                if (cue.time <= targetTime) best = cue;
                else break;
            }}
            return best ? best.dets : [];
        }}

        const colors = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#95e1d3', '#f38181'];
        function getColor(l) {{
            let h = 0;
            for (let i = 0; i < l.length; i++) h = l.charCodeAt(i) + ((h << 5) - h);
            return colors[Math.abs(h) % colors.length];
        }}

        function draw() {{
            document.getElementById('time').textContent = video.currentTime.toFixed(1);
            const dets = findDetections(video.currentTime);
            document.getElementById('count').textContent = dets.length;

            ctx.clearRect(0, 0, canvas.width, canvas.height);
            dets.forEach(det => {{
                const x = det.bbox.x * canvas.width;
                const y = det.bbox.y * canvas.height;
                const w = det.bbox.w * canvas.width;
                const h = det.bbox.h * canvas.height;
                const c = getColor(det.label);

                ctx.strokeStyle = c;
                ctx.lineWidth = 3;
                ctx.strokeRect(x, y, w, h);

                const lbl = `${{det.label}} ${{(det.confidence * 100).toFixed(0)}}%`;
                ctx.font = 'bold 14px Arial';
                ctx.fillStyle = c;
                ctx.fillRect(x, y - 22, ctx.measureText(lbl).width + 10, 22);
                ctx.fillStyle = '#000';
                ctx.fillText(lbl, x + 5, y - 6);
            }});
            requestAnimationFrame(draw);
        }}
        video.addEventListener('playing', () => requestAnimationFrame(draw));
    </script>
</body>
</html>'''
    return web.Response(text=html, content_type='text/html')


async def serve_recording(request):
    name = request.match_info['name']
    filename = request.match_info['file']
    filepath = os.path.join(RECORDINGS_DIR, name, filename)
    if os.path.exists(filepath):
        if filename.endswith('.m3u8'):
            content_type = 'application/vnd.apple.mpegurl'
        elif filename.endswith('.ts'):
            content_type = 'video/mp2t'
        elif filename.endswith('.vtt'):
            content_type = 'text/vtt'
        else:
            content_type = 'application/octet-stream'
        return web.FileResponse(filepath, headers={'Content-Type': content_type})
    return web.Response(status=404)


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


def run_pipeline(pipeline):
    pipeline.run_loop()


async def main():
    global start_time, main_loop

    # Store event loop reference for cross-thread frame pushing
    main_loop = asyncio.get_event_loop()

    if os.path.exists(HLS_DIR):
        shutil.rmtree(HLS_DIR)
    os.makedirs(HLS_DIR)

    print("=" * 60)
    print("  Hailo-8 WebRTC + HLS Server (Low Latency)")
    print("=" * 60)
    print(f"\n  Open: http://<pi-ip>:{HTTP_PORT}\n")
    print("=" * 60)

    pipeline = GStreamerPipeline()
    if not pipeline.start():
        print("[ERROR] Failed to start pipeline!")
        return

    start_time = time.time()

    # Run GStreamer in background thread
    threading.Thread(target=run_pipeline, args=(pipeline,), daemon=True).start()
    time.sleep(2)

    # Web server
    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get('/', index)
    app.router.add_post('/offer', offer)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/start-recording', start_rec)
    app.router.add_get('/stop-recording', stop_rec)
    app.router.add_get('/playback', playback_list)
    app.router.add_get('/playback/{name}', playback_page)
    app.router.add_get('/api/recordings', api_recordings)
    app.router.add_get('/api/events', api_events)
    app.router.add_get('/recordings/{name}/{file}', serve_recording)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()

    print(f"[HTTP] Server on port {HTTP_PORT}")

    # Keep running
    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
