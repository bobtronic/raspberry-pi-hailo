# Raspberry Pi Hailo-8 Real-Time Object Detection

Real-time object detection using YOLOv8 on Raspberry Pi 5 with Hailo-8 AI accelerator. Features ultra-low latency WebRTC streaming and synchronized recording playback.

## Quick Start

```bash
# SSH to the Pi
ssh <user>@<pi-ip>

# Start the server
cd ~/src/raspberry-pi-hailo
python3 webrtc_server.py
```

Open in browser: **http://\<pi-ip\>:8080**

## Features

- **WebRTC Live Stream**: Ultra-low latency (~100-200ms) live video with detection overlays
- **Recording & Playback**: Record sessions with perfectly synchronized detection metadata
- **Server-Side Tracking**: One Euro Filter smoothing + IoU-based object tracking
- **VTT Metadata Sync**: Detection data stored as WebVTT cues for frame-accurate playback

## Hardware

- Raspberry Pi 5 (8GB, Debian 12 Bookworm)
- Hailo-8 AI Hat (26 TOPS)
- Camera Module

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       webrtc_server.py                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Camera (I420 1280x720 @ 30fps)                                     │
│         │                                                            │
│      ┌──┴──┐                                                        │
│      │ tee │─────────────────────────────┐                          │
│      └──┬──┘                             │                          │
│         │                                │                          │
│         ▼                                ▼                          │
│  ┌─────────────┐                  ┌─────────────┐                   │
│  │  INFERENCE  │                  │    VIDEO    │                   │
│  │   BRANCH    │                  │   BRANCH    │                   │
│  ├─────────────┤                  ├─────────────┤                   │
│  │ I420 → RGB  │                  │ I420 → H.264│ (single encoder)  │
│  │ scale 640²  │                  │      │      │                   │
│  │ hailonet    │                  │   ┌──┴──┐   │                   │
│  │ hailofilter │                  │   │ tee │   │                   │
│  │      │      │                  │   └──┬──┘   │                   │
│  │      ▼      │                  │  ┌───┴───┐  │                   │
│  │  Detections │                  │  ▼       ▼  │                   │
│  │  (VTT sync) │                  │WebRTC   HLS │                   │
│  └─────────────┘                  └─────────────┘                   │
│                                                                      │
│  Detection Tracker: One Euro Filter + IoU matching + hysteresis     │
│  HTTP :8080 ─── Web UI, WebRTC signaling, recordings                │
│  WS   :8080 ─── Live detection JSON stream                          │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Pipeline optimizations:**
- Single H.264 encoder shared by WebRTC and HLS
- No redundant colorspace conversions
- Inference branch isolated (doesn't affect video path)
- ~70% CPU, ~170 MB RAM

## Recording Format

Recordings are stored in `~/recordings/<timestamp>/`:

| File | Description |
|------|-------------|
| `playlist.m3u8` | HLS playlist (VOD) |
| `segment*.ts` | Video segments (1 sec each) |
| `detections.vtt` | WebVTT file with detection metadata |

### VTT Detection Format

Detection data is stored as WebVTT cues with JSON payloads:

```vtt
WEBVTT
Kind: metadata

00:00:01.234 --> 00:00:01.284
[{"label":"person","confidence":0.85,"bbox":{"x":0.1,"y":0.2,"w":0.3,"h":0.5}}]

00:00:01.267 --> 00:00:01.317
[{"label":"person","confidence":0.86,"bbox":{"x":0.11,"y":0.21,"w":0.3,"h":0.5}}]
```

The browser's TextTrack API automatically syncs cues with video playback.

## Project Files

| File | Description |
|------|-------------|
| `webrtc_server.py` | Main server - WebRTC, HLS, detection tracking |
| `hailo_inference_engine.py` | Hailo device management (legacy) |
| `gstreamer_h264_server.py` | HLS-only server (reference) |

## Performance

| Metric | Value |
|--------|-------|
| Live latency | ~100-200ms (WebRTC) |
| Detection FPS | ~30 FPS |
| Video resolution | 1280x720 @ 30fps |
| Model | YOLOv8s (80 COCO classes) |

## Usage

### Start the Server

```bash
# Foreground (see logs)
python3 webrtc_server.py

# Background
nohup python3 -u webrtc_server.py > /tmp/webrtc.log 2>&1 &
```

### View Logs

```bash
tail -f /tmp/webrtc.log
```

### Stop the Server

```bash
pkill -f webrtc_server
```

## Detection Tracking

The server applies tracking before sending detections to clients:

1. **Confidence Filter**: Only detections above MIN_CONFIDENCE (0.4) are tracked
2. **IoU Matching**: New detections matched to existing tracks by bounding box overlap
3. **One Euro Filter**: Adaptive smoothing - responsive when moving, stable when still
4. **Hysteresis**: Objects must be seen MIN_HITS (8) times before showing, kept for MAX_AGE (15) frames after disappearing

## Dependencies

```bash
pip install aiortc aiohttp av numpy
```

System packages: `gstreamer1.0-tools`, `gstreamer1.0-plugins-*`, `hailo-tappas-core`

## Troubleshooting

### WebRTC Not Connecting

Check that port 8080 is accessible and no firewall is blocking UDP.

### Camera Busy

```bash
sudo pkill -9 rpicam
sudo pkill -9 python3
```

### Hailo Device Busy

```bash
sudo systemctl restart hailort
```

## License

MIT
