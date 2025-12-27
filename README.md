# Raspberry Pi Hailo-8 Real-Time Object Detection

Real-time object detection using YOLOv8 on Raspberry Pi 5 with Hailo-8 AI accelerator. Features ultra-low latency WebRTC streaming, a dora-rs dataflow pipeline with rules engine, and synchronized recording playback.

## Quick Start

### Option 1: Dora Pipeline (Recommended)

```bash
ssh <user>@<pi-ip>
cd ~/src/raspberry-pi-hailo/inference-pipeline
dora up && dora start dataflow.yaml
```

### Option 2: Standalone Server

```bash
ssh <user>@<pi-ip>
cd ~/src/raspberry-pi-hailo
python3 webrtc_server.py
```

Open in browser: **http://\<pi-ip\>:8080**

## Features

**Both versions:**
- **WebRTC Live Stream**: Ultra-low latency (~100-200ms) live video with detection overlays
- **Server-Side Tracking**: One Euro Filter smoothing + IoU-based object tracking
- **YOLOv8 on Hailo-8**: 30 FPS inference at 1280x720

**Standalone (`webrtc_server.py`):**
- **HLS Recording**: Continuous recording to disk with 1-second segments
- **VTT Metadata**: Detection data synced to video for frame-accurate playback
- **Recording Playback**: Built-in web UI for browsing and playing recordings

**Dora Pipeline (`inference-pipeline/`):**
- **Dora-rs Dataflow**: Modular pipeline with zero-copy Arrow IPC messaging
- **Rules Engine**: YAML-configurable event detection (zones, velocity, loitering)
- **Tracker State**: Enriched detection data with velocity and track history

## Hardware

- Raspberry Pi 5 (8GB, Debian 12 Bookworm)
- Hailo-8 AI Hat (26 TOPS)
- Camera Module

---

## Dora-rs Pipeline

The `inference-pipeline/` directory contains a modular dataflow architecture using [dora-rs](https://dora-rs.ai/).

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Dora Dataflow Pipeline                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                      gst-bridge (WebRTC)                             │    │
│  │  ┌─────────────┐                                                     │    │
│  │  │  GStreamer  │──► Camera 1280x720@30fps                           │    │
│  │  │   Pipeline  │                                                     │    │
│  │  └──────┬──────┘                                                     │    │
│  │         │                                                            │    │
│  │      ┌──┴──┐                                                         │    │
│  │      │ tee │────────────────────────────┐                            │    │
│  │      └──┬──┘                            │                            │    │
│  │         │                               │                            │    │
│  │         ▼                               ▼                            │    │
│  │  ┌─────────────┐                 ┌─────────────┐                     │    │
│  │  │  INFERENCE  │                 │   WEBRTC    │                     │    │
│  │  │   BRANCH    │                 │   BRANCH    │                     │    │
│  │  ├─────────────┤                 ├─────────────┤                     │    │
│  │  │ hailonet    │                 │ RGB appsink │──► HailoVideoTrack  │    │
│  │  │ hailofilter │                 │             │        │            │    │
│  │  │      │      │                 │             │        ▼            │    │
│  │  │      ▼      │                 │             │    aiortc/aiohttp   │    │
│  │  │  Detections │                 │             │    :8080 WebRTC     │    │
│  │  └──────┬──────┘                 └─────────────┘                     │    │
│  │         │                                                            │    │
│  └─────────┼────────────────────────────────────────────────────────────┘    │
│            │                                                                 │
│            │ Arrow StructArray (zero-copy shared memory)                     │
│            ▼                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        tracker-state                                 │    │
│  │  • Maintains track history (last 30 positions per object)           │    │
│  │  • Computes velocity vectors                                         │    │
│  │  • Manages track lifecycle (new → active → lost)                    │    │
│  │  • Enriches detections with temporal metadata                        │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│            │                                                                 │
│            │ Arrow StructArray (zero-copy shared memory)                     │
│            ▼                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        rules-engine                                  │    │
│  │  • Loads rules from YAML configuration                               │    │
│  │  • Evaluates zone presence (polygon point-in-polygon)               │    │
│  │  • Detects velocity thresholds                                       │    │
│  │  • Identifies stationary/loitering objects                          │    │
│  │  • Emits events back to gst-bridge for WebSocket broadcast          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│            │                                                                 │
│            │ events (Arrow StructArray)                                      │
│            ▼                                                                 │
│       WebSocket broadcast to browser UI                                      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Dataflow Definition

```yaml
nodes:
  - id: gst-bridge
    path: nodes/gst_bridge_webrtc.py
    inputs:
      tick: dora/timer/millis/33      # 30 FPS processing
      events: rules-engine/events      # Events for WebSocket
    outputs: [detections]

  - id: tracker-state
    path: nodes/tracker_state.py
    inputs:
      detections: gst-bridge/detections
    outputs: [tracks]

  - id: rules-engine
    path: nodes/rules_engine.py
    inputs:
      tracks: tracker-state/tracks
    outputs: [events]
```

### Zero-Copy IPC

Dora uses Apache Arrow for inter-process communication. Data flows between nodes via shared memory with zero serialization overhead:

```python
# Sender: Pass Arrow array directly
node.send_output("detections", pa.array(detection_structs))

# Receiver: Direct access to shared memory
detections = event["value"].to_pylist()
```

### Rules Configuration

Rules are defined in `inference-pipeline/rules/default.yaml`:

```yaml
rules:
  # Zone presence detection
  - name: person_in_center
    trigger:
      class: person
      zone: [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
      min_duration_sec: 1.0
    action:
      type: log
      message: "Person in center zone (track {track_id})"
    cooldown_sec: 5.0

  # Velocity-based detection
  - name: fast_motion
    trigger:
      min_velocity: 0.3
    action:
      type: log
      message: "Fast moving {class_name} (speed: {speed:.2f})"
    cooldown_sec: 3.0

  # Loitering detection
  - name: stationary_object
    trigger:
      stationary_duration_sec: 10.0
      velocity_threshold: 0.005
    action:
      type: log
      message: "Stationary {class_name} for {duration_sec:.1f}s"
    cooldown_sec: 30.0
```

**Available Triggers:**
| Trigger | Description |
|---------|-------------|
| `class` | Filter by object class (person, car, etc.) |
| `zone` | Polygon coordinates (normalized 0-1) |
| `min_duration_sec` | Time in zone before triggering |
| `min_velocity` / `max_velocity` | Speed thresholds |
| `stationary_duration_sec` | Loitering detection |
| `min_age_sec` | Track persistence threshold |

### Resource Consumption

CPU percentages are relative to a single core (Pi 5 has 4 cores, so 400% = full utilization).

**Idle (no WebRTC viewer connected):**
| Configuration | CPU | Memory | Notes |
|---------------|-----|--------|-------|
| **Standalone** | ~119% | ~350 MB | Includes HLS recording to disk |
| **Dora Pipeline** | ~94% | ~460 MB | Tracker + rules engine |

**Active (WebRTC viewer connected):**
| Configuration | CPU | Memory | Notes |
|---------------|-----|--------|-------|
| **Standalone** | ~191% | ~465 MB | +72% for WebRTC encoding |
| **Dora Pipeline** | ~173% | ~620 MB | +79% for WebRTC encoding |

Process breakdown (Dora, idle):
| Node | CPU | Memory | Function |
|------|-----|--------|----------|
| gst-bridge | ~74% | 208 MB | GStreamer + Hailo + WebRTC server |
| tracker-state | ~5% | 71 MB | Track history & velocity |
| rules-engine | ~9% | 71 MB | Rule evaluation |
| rpicam-vid | ~7% | 112 MB | Camera capture |

**Notes:**
- WebRTC viewer adds ~72-79% CPU overhead (video encoding + DTLS/SRTP encryption)
- Standalone includes HLS recording and playback server; Dora version is WebRTC-only
- Dora idle is lower CPU than standalone because standalone does continuous HLS encoding to disk

---

## Standalone Architecture

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

### Dora Pipeline (`inference-pipeline/`)

| File | Description |
|------|-------------|
| `dataflow.yaml` | Dora dataflow definition |
| `gst_hailo_pipeline.py` | GStreamer pipeline with Hailo integration |
| `nodes/gst_bridge_webrtc.py` | Bridge node: GStreamer + WebRTC + Dora |
| `nodes/tracker_state.py` | Track history and velocity computation |
| `nodes/rules_engine.py` | YAML-based rule evaluation |
| `rules/default.yaml` | Default rule definitions |

### Standalone

| File | Description |
|------|-------------|
| `webrtc_server.py` | Standalone server - WebRTC, HLS, detection tracking |
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

### Dora Pipeline

```bash
# Install dora CLI
curl -sSf https://raw.githubusercontent.com/dora-rs/dora/main/install.sh | bash

# Python packages
pip install dora-rs pyarrow aiohttp aiortc av numpy pyyaml
```

### Standalone

```bash
pip install aiortc aiohttp av numpy
```

### System Packages

```bash
apt install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad hailo-tappas-core
```

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
