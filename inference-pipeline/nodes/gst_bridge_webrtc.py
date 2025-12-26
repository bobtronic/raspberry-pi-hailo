#!/usr/bin/env python3
"""
GStreamer-Dora Bridge with WebRTC Server

Combines the Hailo inference pipeline with:
- Dora dataflow for detection analytics
- WebRTC streaming for live video
- WebSocket for real-time detection/event data
"""
import sys
import os
import threading
import queue
import asyncio
import time
import json
import fractions

# Add parent directory to path for gst_hailo_pipeline import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pyarrow as pa
from dora import Node

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

from gst_hailo_pipeline import GstHailoPipeline


class HailoVideoTrack(VideoStreamTrack):
    """Video track that serves frames from the GStreamer pipeline."""

    kind = "video"

    def __init__(self, frame_queue, frame_lock, get_latest_frame):
        super().__init__()
        self._start = time.time()
        self._frame_queue = frame_queue
        self._frame_lock = frame_lock
        self._get_latest_frame = get_latest_frame

    async def recv(self):
        try:
            frame_data = await asyncio.wait_for(
                self._frame_queue.get(), timeout=0.1
            )
        except asyncio.TimeoutError:
            with self._frame_lock:
                frame_data = self._get_latest_frame()

        if frame_data is not None:
            frame = VideoFrame.from_ndarray(frame_data, format="rgb24")
        else:
            # Black frame as fallback
            frame = VideoFrame.from_ndarray(
                np.zeros((720, 1280, 3), dtype=np.uint8), format="rgb24"
            )

        elapsed = time.time() - self._start
        frame.pts = int(elapsed * 90000)
        frame.time_base = fractions.Fraction(1, 90000)
        return frame


class GstBridgeWebRTCNode:
    """
    Dora node bridging GStreamer Hailo pipeline to Dora dataflow with WebRTC.

    Outputs:
        - detections: Arrow RecordBatch with detection data

    Inputs:
        - tick: Timer to process detection queue
        - events: Events from rules_engine to forward to WebSocket
    """


    # COCO class mapping
    CLASS_MAP = {
        'person': 0, 'bicycle': 1, 'car': 2, 'motorcycle': 3, 'airplane': 4,
        'bus': 5, 'train': 6, 'truck': 7, 'boat': 8, 'traffic light': 9,
        'fire hydrant': 10, 'stop sign': 11, 'parking meter': 12, 'bench': 13,
        'bird': 14, 'cat': 15, 'dog': 16, 'horse': 17, 'sheep': 18, 'cow': 19,
        'elephant': 20, 'bear': 21, 'zebra': 22, 'giraffe': 23, 'backpack': 24,
        'umbrella': 25, 'handbag': 26, 'tie': 27, 'suitcase': 28, 'frisbee': 29,
        'skis': 30, 'snowboard': 31, 'sports ball': 32, 'kite': 33,
        'baseball bat': 34, 'baseball glove': 35, 'skateboard': 36,
        'surfboard': 37, 'tennis racket': 38, 'bottle': 39, 'wine glass': 40,
        'cup': 41, 'fork': 42, 'knife': 43, 'spoon': 44, 'bowl': 45,
        'banana': 46, 'apple': 47, 'sandwich': 48, 'orange': 49, 'broccoli': 50,
        'carrot': 51, 'hot dog': 52, 'pizza': 53, 'donut': 54, 'cake': 55,
        'chair': 56, 'couch': 57, 'potted plant': 58, 'bed': 59,
        'dining table': 60, 'toilet': 61, 'tv': 62, 'laptop': 63, 'mouse': 64,
        'remote': 65, 'keyboard': 66, 'cell phone': 67, 'microwave': 68,
        'oven': 69, 'toaster': 70, 'sink': 71, 'refrigerator': 72, 'book': 73,
        'clock': 74, 'vase': 75, 'scissors': 76, 'teddy bear': 77,
        'hair drier': 78, 'toothbrush': 79
    }

    def __init__(self):
        self.node = Node()
        self.pipeline = None
        self._detection_queue = queue.Queue(maxsize=10)
        self._running = True

        # WebRTC state
        self._frame_queue = None  # asyncio.Queue, created in asyncio thread
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._asyncio_loop = None
        self._pcs = set()  # Active peer connections

        # Detection state for WebSocket
        self._latest_detections = {"detections": [], "fps": 0, "timestamp": 0}
        self._detection_lock = threading.Lock()
        self._events_buffer = []
        self._frame_count = 0
        self._start_time = time.time()

        # Config from environment
        self.width = int(os.environ.get('CAM_WIDTH', '1280'))
        self.height = int(os.environ.get('CAM_HEIGHT', '720'))
        self.fps = int(os.environ.get('CAM_FPS', '30'))
        self.webrtc_port = int(os.environ.get('WEBRTC_PORT', '8080'))
        self.enable_webrtc = os.environ.get('ENABLE_WEBRTC', 'true').lower() == 'true'

    def _on_detection(self, detections, frame_id, timestamp_ms):
        """Callback from GStreamer pipeline - queues detections."""
        try:
            self._detection_queue.put_nowait((detections, frame_id, timestamp_ms))
        except queue.Full:
            pass

        # Update detection state for WebSocket
        self._frame_count += 1
        elapsed = time.time() - self._start_time
        fps = self._frame_count / elapsed if elapsed > 0 else 0

        with self._detection_lock:
            self._latest_detections = {
                "detections": detections,
                "fps": round(fps, 1),
                "timestamp": round(elapsed, 3),
                "events": [e.get('message', '') for e in self._events_buffer[-5:]]
            }

    def _on_frame(self, frame_data):
        """Callback from GStreamer pipeline - queues frame for WebRTC."""
        with self._frame_lock:
            self._latest_frame = frame_data

        if self._asyncio_loop is not None:
            try:
                self._asyncio_loop.call_soon_threadsafe(
                    lambda: self._frame_queue.put_nowait(frame_data)
                    if self._frame_queue and not self._frame_queue.full() else None
                )
            except Exception:
                pass

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

    # ==================== WebRTC Server ====================

    async def _index(self, request):
        """Serve the web UI."""
        html = '''<!DOCTYPE html>
<html>
<head>
    <title>Hailo Dora WebRTC</title>
    <style>
        body { margin: 0; padding: 20px; background: #1a1a1a; color: #fff; font-family: Arial; text-align: center; }
        h1 { color: #00ff88; margin-bottom: 5px; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 20px; }
        .main-layout { display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; }
        #container { position: relative; display: inline-block; }
        #video { max-width: 1280px; width: 100%; display: block; border: 3px solid #333; border-radius: 8px; background: #000; }
        #canvas { position: absolute; top: 3px; left: 3px; pointer-events: none; }
        .info { margin-top: 10px; padding: 15px; background: #2a2a2a; border-radius: 8px; text-align: left; }
        .label { color: #00ff88; font-weight: bold; display: inline-block; width: 80px; }
        .status-ok { color: #00ff88; }
        button { font-size: 12px; padding: 4px 12px; border: none; border-radius: 4px; cursor: pointer; margin: 2px; background: #0066cc; color: #fff; }
        .events-panel { width: 300px; background: #2a2a2a; border-radius: 8px; padding: 15px; text-align: left; max-height: 500px; overflow-y: auto; }
        .events-panel h3 { color: #00ff88; margin: 0 0 10px 0; font-size: 14px; }
        .event-item { padding: 8px; margin: 4px 0; background: #333; border-radius: 4px; font-size: 12px; border-left: 3px solid #4ecdc4; }
        .event-time { color: #666; font-size: 10px; }
    </style>
</head>
<body>
    <h1>Hailo-8 + Dora WebRTC</h1>
    <div class="subtitle">Dora dataflow with WebRTC streaming</div>
    <div class="main-layout">
        <div id="container">
            <video id="video" autoplay muted playsinline></video>
            <canvas id="canvas"></canvas>
            <div class="info">
                <div><span class="label">Status:</span> <span id="status">Connecting...</span></div>
                <div><span class="label">FPS:</span> <span id="fps">0</span></div>
                <div><span class="label">Objects:</span> <span id="count">0</span></div>
                <div><button onclick="connect()">Reconnect</button></div>
            </div>
        </div>
        <div class="events-panel">
            <h3>Live Events (Dora)</h3>
            <div id="events">Waiting for events...</div>
        </div>
    </div>
    <script>
        const video = document.getElementById('video');
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d');
        let pc = null, ws = null, allEvents = [];

        const colors = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#95e1d3', '#f38181'];
        function getColor(l) { let h=0; for(let i=0;i<l.length;i++) h=l.charCodeAt(i)+((h<<5)-h); return colors[Math.abs(h)%colors.length]; }

        function resizeCanvas() { const r=video.getBoundingClientRect(); canvas.width=r.width; canvas.height=r.height; }
        video.addEventListener('loadedmetadata', resizeCanvas);
        window.addEventListener('resize', resizeCanvas);

        function renderEvents() {
            const el = document.getElementById('events');
            if (!allEvents.length) { el.innerHTML = '<div style="color:#666">No events yet</div>'; return; }
            el.innerHTML = allEvents.slice(-15).reverse().map(e =>
                `<div class="event-item"><div class="event-time">${new Date(e.ts*1000).toLocaleTimeString()}</div>${e.msg}</div>`
            ).join('');
        }

        async function connect() {
            document.getElementById('status').textContent = 'Connecting...';
            pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
            pc.ontrack = e => { video.srcObject = e.streams[0]; document.getElementById('status').textContent = 'Connected'; document.getElementById('status').className = 'status-ok'; resizeCanvas(); };
            pc.oniceconnectionstatechange = () => { if (pc.iceConnectionState === 'failed') document.getElementById('status').textContent = 'Failed'; };
            pc.addTransceiver('video', {direction: 'recvonly'});
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            await new Promise(r => { if (pc.iceGatheringState === 'complete') r(); else pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') r(); }; });
            const resp = await fetch('/offer', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({sdp: pc.localDescription.sdp, type: pc.localDescription.type}) });
            const answer = await resp.json();
            await pc.setRemoteDescription(new RTCSessionDescription(answer));
            connectWS();
        }

        function connectWS() {
            ws = new WebSocket(`ws://${location.host}/ws`);
            ws.onmessage = e => {
                const d = JSON.parse(e.data);
                document.getElementById('fps').textContent = d.fps || 0;
                const dets = d.detections || [];
                document.getElementById('count').textContent = dets.length;
                if (d.events) d.events.forEach(msg => { if(msg) allEvents.push({ts: Date.now()/1000, msg}); });
                if (allEvents.length > 50) allEvents = allEvents.slice(-50);
                renderEvents();
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                dets.forEach(det => {
                    const x=det.bbox.x*canvas.width, y=det.bbox.y*canvas.height, w=det.bbox.w*canvas.width, h=det.bbox.h*canvas.height, c=getColor(det.label);
                    ctx.strokeStyle=c; ctx.lineWidth=3; ctx.strokeRect(x,y,w,h);
                    const lbl=`${det.label} ${(det.confidence*100).toFixed(0)}%`;
                    ctx.font='bold 14px Arial'; ctx.fillStyle=c; ctx.fillRect(x,y-22,ctx.measureText(lbl).width+10,22);
                    ctx.fillStyle='#000'; ctx.fillText(lbl,x+5,y-6);
                });
            };
            ws.onclose = () => setTimeout(connectWS, 1000);
        }
        connect();
    </script>
</body>
</html>'''
        return web.Response(text=html, content_type='text/html')

    async def _offer(self, request):
        """Handle WebRTC offer."""
        params = await request.json()
        offer = RTCSessionDescription(sdp=params['sdp'], type=params['type'])

        pc = RTCPeerConnection()
        self._pcs.add(pc)

        @pc.on('connectionstatechange')
        async def on_state():
            if pc.connectionState == 'failed':
                await pc.close()
                self._pcs.discard(pc)

        video_track = HailoVideoTrack(
            self._frame_queue,
            self._frame_lock,
            lambda: self._latest_frame
        )
        pc.addTrack(video_track)

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.json_response({
            'sdp': pc.localDescription.sdp,
            'type': pc.localDescription.type
        })

    async def _websocket(self, request):
        """WebSocket handler for detection data."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        try:
            while True:
                with self._detection_lock:
                    data = self._latest_detections.copy()
                await ws.send_json(data)
                await asyncio.sleep(0.033)
        except Exception:
            pass

        return ws

    async def _api_events(self, request):
        """Return recent events."""
        return web.json_response(self._events_buffer[-50:])

    async def _on_shutdown(self, app):
        """Clean up peer connections."""
        coros = [pc.close() for pc in self._pcs]
        await asyncio.gather(*coros)
        self._pcs.clear()

    async def _run_server(self):
        """Run the aiohttp server."""
        self._frame_queue = asyncio.Queue(maxsize=2)

        app = web.Application()
        app.on_shutdown.append(self._on_shutdown)
        app.router.add_get('/', self._index)
        app.router.add_post('/offer', self._offer)
        app.router.add_get('/ws', self._websocket)
        app.router.add_get('/api/events', self._api_events)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.webrtc_port)
        await site.start()

        print(f"[WebRTC] Server running on http://0.0.0.0:{self.webrtc_port}")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

    def _run_asyncio_thread(self):
        """Run asyncio event loop in background thread."""
        self._asyncio_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._asyncio_loop)
        self._asyncio_loop.run_until_complete(self._run_server())

    def run(self):
        """Main loop - starts pipeline, WebRTC server, and handles Dora events."""
        print(f"[GstBridgeWebRTC] Starting pipeline: {self.width}x{self.height}@{self.fps}fps")
        print(f"[GstBridgeWebRTC] WebRTC enabled: {self.enable_webrtc}")

        # Start asyncio server thread if WebRTC enabled
        if self.enable_webrtc:
            asyncio_thread = threading.Thread(target=self._run_asyncio_thread, daemon=True)
            asyncio_thread.start()
            time.sleep(1)

        # Start GStreamer pipeline
        self.pipeline = GstHailoPipeline(
            width=self.width,
            height=self.height,
            fps=self.fps,
            on_detection=self._on_detection,
            on_frame=self._on_frame if self.enable_webrtc else None,
            enable_webrtc=self.enable_webrtc
        )

        if not self.pipeline.start():
            print("[GstBridgeWebRTC] Failed to start pipeline")
            return

        # Run GStreamer loop in background
        gst_thread = self.pipeline.run_threaded()
        time.sleep(1)

        print("[GstBridgeWebRTC] Processing Dora events...")

        try:
            for event in self.node:
                event_type = event["type"]

                if event_type == "INPUT":
                    input_id = event["id"]

                    if input_id == "tick":
                        self._process_queue()

                    elif input_id == "events":
                        # Receive events from rules_engine - zero-copy Arrow
                        try:
                            # Direct access to Arrow StructArray - no IPC deserialization
                            events_list = event["value"].to_pylist()
                            for evt_struct in events_list:
                                evt = {
                                    'timestamp': evt_struct['timestamp_ms'],
                                    'rule_name': evt_struct['rule_name'],
                                    'message': evt_struct['message'],
                                    'type': evt_struct['event_type'],
                                }
                                self._events_buffer.append(evt)

                            # Keep last 100 events
                            if len(self._events_buffer) > 100:
                                self._events_buffer = self._events_buffer[-100:]
                        except Exception as e:
                            print(f"[GstBridgeWebRTC] Events parse error: {e}")

                elif event_type == "STOP":
                    print("[GstBridgeWebRTC] Received STOP signal")
                    break

        except KeyboardInterrupt:
            print("[GstBridgeWebRTC] Interrupted")
        finally:
            self._running = False
            self.pipeline.stop()
            print("[GstBridgeWebRTC] Shutdown complete")


def main():
    node = GstBridgeWebRTCNode()
    node.run()


if __name__ == '__main__':
    main()
