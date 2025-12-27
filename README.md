# Raspberry Pi Hailo-8 Real-Time Object Detection

Real-time object detection using YOLOv8 on Raspberry Pi 5 with Hailo-8 AI accelerator. Features ultra-low latency WebRTC streaming with two implementation options.

## Implementations

| | [HLS Recorder](hls-recorder/) | [Dora Dataflow](dora-dataflow/) |
|---|---|---|
| **Architecture** | Single Python file | Modular dora-rs dataflow |
| **Best For** | Simple deployment, recording | Event-driven applications |
| **WebRTC Live Stream** | Yes | Yes |
| **HLS Recording** | Yes (continuous) | No |
| **Recording Playback** | Yes (web UI) | No |
| **Rules Engine** | No | Yes (YAML-configurable) |
| **Track Velocity** | No | Yes |
| **Zone Detection** | No | Yes |
| **Loitering Detection** | No | Yes |

### Resource Usage

CPU percentages are relative to a single core (Pi 5 has 4 cores = 400% max).

| | No Viewer | With WebRTC Viewer |
|---|---|---|
| **HLS Recorder** | ~119% CPU, 350 MB | ~191% CPU, 465 MB |
| **Dora Dataflow** | ~94% CPU, 460 MB | ~173% CPU, 620 MB |

HLS Recorder uses more CPU when idle because it continuously encodes HLS to disk.

## Quick Start

### Option 1: Dora Dataflow

```bash
cd ~/src/raspberry-pi-hailo/dora-dataflow
dora up && dora start dataflow.yaml
```

### Option 2: HLS Recorder

```bash
cd ~/src/raspberry-pi-hailo/hls-recorder
python3 webrtc_server.py
```

Open in browser: **http://\<pi-ip\>:8080**

## Hardware

- Raspberry Pi 5 (8GB, Debian 12 Bookworm)
- Hailo-8 AI Hat (26 TOPS)
- Camera Module

## Performance

| Metric | Value |
|--------|-------|
| Live latency | ~100-200ms (WebRTC) |
| Detection FPS | ~30 FPS |
| Video resolution | 1280x720 @ 30fps |
| Model | YOLOv8s (80 COCO classes) |

## Dependencies

### System Packages

```bash
apt install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad hailo-tappas-core
```

### Python (Standalone)

```bash
pip install aiortc aiohttp av numpy
```

### Python (Dora Pipeline)

```bash
# Install dora CLI
curl -sSf https://raw.githubusercontent.com/dora-rs/dora/main/install.sh | bash

# Python packages
pip install dora-rs pyarrow aiohttp aiortc av numpy pyyaml
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
