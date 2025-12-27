# Raspberry Pi 5 + Hailo-8 Real-Time Object Detection

**30 FPS YOLOv8 on a $100 board. WebRTC streaming with 150ms latency. No cloud required.**

Transform a Raspberry Pi 5 into a real-time AI camera using the Hailo-8 accelerator. Detect 80 object classes at full frame rate while streaming to any browser—all running locally on 5 watts.

```
Camera → Hailo-8 NPU → WebRTC → Browser
          (30 FPS)      (150ms)   (live overlays)
```

## Why This Exists

Edge AI inference is finally fast and cheap. A Raspberry Pi 5 with Hailo-8 delivers **26 TOPS** of neural network performance for under $150 total. But getting video, inference, and streaming to work together efficiently is hard. This project solves that:

- **GStreamer + Hailo pipeline** — Optimized for zero-copy frame handling
- **WebRTC streaming** — Sub-200ms latency to any browser, no plugins
- **Client-side rendering** — Detections sent via WebSocket; browser draws overlays on canvas
- **Smooth tracking** — One Euro Filter eliminates jitter without adding latency

## Two Implementations

| | [HLS Recorder](hls-recorder/) | [Dora Dataflow](dora-dataflow/) |
|---|---|---|
| **What it does** | Live view + continuous recording | Live view + event detection |
| **Architecture** | Single Python file | Modular dataflow nodes |
| **Recording** | HLS segments + VTT metadata | — |
| **Playback** | Web UI with synced overlays | — |
| **Tracking** | Position smoothing | Position + velocity |
| **Rules engine** | — | Zone, speed, loitering alerts |
| **IPC** | — | Zero-copy Arrow shared memory |

### HLS Recorder

Records 24/7 while streaming live. Detection metadata stored as WebVTT cues, so playback has frame-accurate overlays using the browser's native TextTrack API. One file, one command, works out of the box.

### Dora Dataflow

Offloads detection post-processing to separate Python nodes using [dora-rs](https://dora-rs.ai/). Apache Arrow arrays flow between processes via shared memory—no serialization overhead. Define rules in YAML: "alert when person enters zone for >5 seconds" or "detect objects moving faster than threshold."

## Quick Start

```bash
git clone https://github.com/anthropics/raspberry-pi-hailo
cd raspberry-pi-hailo

# Option 1: Recording + Playback
cd hls-recorder && python3 webrtc_server.py

# Option 2: Rules + Events
cd dora-dataflow && dora up && dora start dataflow.yaml
```

Open **http://\<raspberry-pi-ip\>:8080**

## Performance

| Metric | Value |
|--------|-------|
| Inference | **30 FPS** sustained |
| Latency | **~150ms** camera to browser |
| Resolution | 1280×720 @ 30fps |
| Model | YOLOv8s (80 COCO classes) |
| Power | ~5W total system |

### CPU Usage

Percentages are per-core (Pi 5 = 4 cores = 400% max).

| | Idle | Streaming |
|---|---|---|
| **HLS Recorder** | 119% | 191% |
| **Dora Dataflow** | 94% | 173% |

HLS Recorder encodes to disk continuously. Dora only encodes when a viewer connects.

## Hardware

- **Raspberry Pi 5** (8GB recommended)
- **Hailo-8L AI Hat** — 26 TOPS neural accelerator
- **Camera Module** — Any Pi-compatible camera
- **Cooling** — Heatsink or active fan recommended

## Installation

```bash
# System packages
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad hailo-tappas-core

# Python (HLS Recorder)
pip install aiortc aiohttp av numpy

# Python (Dora Dataflow)
curl -sSf https://raw.githubusercontent.com/dora-rs/dora/main/install.sh | bash
pip install dora-rs pyarrow aiohttp aiortc av numpy pyyaml
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Raspberry Pi 5                            │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   rpicam-vid ──► GStreamer ──┬──► Hailo-8 NPU ──► Detections     │
│                (I420 720p)   │      (YOLOv8s)         │          │
│                              │                        │          │
│                              │                        ▼          │
│                              └──► H.264 Encoder ──► WebRTC ◄─────┤
│                                        │              │          │
│                                        ▼              │          │
│                                   HLS Segments   WebSocket       │
│                                   + VTT metadata  (JSON)         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────┐
│                          Browser                                 │
├──────────────────────────────────────────────────────────────────┤
│   <video> ◄── WebRTC          Canvas overlay ◄── WebSocket       │
│           (H.264 stream)                     (detection boxes)   │
└──────────────────────────────────────────────────────────────────┘
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| WebRTC won't connect | Check port 8080, ensure UDP not blocked |
| Camera in use | `sudo pkill -9 rpicam; sudo pkill -9 python3` |
| Hailo not responding | `sudo systemctl restart hailort` |
| High latency | Check network; try wired ethernet |

## License

MIT
