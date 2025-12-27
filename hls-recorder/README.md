# HLS Recorder

Single-file server for real-time object detection with WebRTC streaming and HLS recording.

## Quick Start

```bash
cd ~/src/raspberry-pi-hailo/hls-recorder
python3 webrtc_server.py
```

Open in browser: **http://\<pi-ip\>:8080**

## Features

- **WebRTC Live Stream**: Ultra-low latency (~100-200ms) video with detection overlays
- **HLS Recording**: Continuous recording to disk with 1-second segments
- **VTT Metadata**: Detection data synced to video for frame-accurate playback
- **Recording Playback**: Built-in web UI for browsing and playing past recordings
- **Server-Side Tracking**: One Euro Filter smoothing + IoU-based object tracking

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

### Design Decisions

**Tee at raw I420:** Split immediately after camera input, before colorspace conversion. Each branch only does conversions it needs - no redundant processing.

**Separate inference branch:** Inference runs at 640x640 (model input size) while video stays at 1280x720. No wasteful upscaling/downscaling.

**WebVTT for detection sync:** Browser's TextTrack API provides automatic sync with video playback. Seeking works correctly with no manual timestamp matching.

**aiortc for WebRTC:** Simpler integration with aiohttp than GStreamer's webrtcbin. Trade-off: Python re-encodes frames (future optimization could use H.264 passthrough).

## Resource Usage

CPU percentages are relative to a single core (Pi 5 has 4 cores, so 400% = full utilization).

| Scenario | CPU | Memory |
|----------|-----|--------|
| No viewer (HLS recording only) | ~119% | ~350 MB |
| With WebRTC viewer | ~191% | ~465 MB |

WebRTC encoding adds ~72% CPU overhead.

## Recording Format

Recordings are stored in `~/recordings/<timestamp>/`:

| File | Description |
|------|-------------|
| `playlist.m3u8` | HLS playlist (VOD) |
| `segment*.ts` | Video segments (1 sec each) |
| `detections.vtt` | WebVTT file with detection metadata |

### VTT Detection Format

```vtt
WEBVTT
Kind: metadata

00:00:01.234 --> 00:00:01.284
[{"label":"person","confidence":0.85,"bbox":{"x":0.1,"y":0.2,"w":0.3,"h":0.5}}]
```

The browser's TextTrack API automatically syncs cues with video playback.

## Detection Tracking

Server-side tracking applied before sending to clients:

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `MIN_CONFIDENCE` | 0.4 | Filter weak detections |
| `IOU_THRESHOLD` | 0.3 | Match detections to tracks |
| `MIN_HITS` | 8 | Frames before showing (hysteresis) |
| `MAX_AGE` | 15 | Frames before removing |
| `EURO_MIN_CUTOFF` | 0.5 | One Euro Filter smoothing |
| `EURO_BETA` | 0.02 | One Euro Filter responsiveness |

## Usage

```bash
# Foreground (see logs)
python3 webrtc_server.py

# Background
nohup python3 -u webrtc_server.py > /tmp/webrtc.log 2>&1 &

# View logs
tail -f /tmp/webrtc.log

# Stop
pkill -f webrtc_server
```

## Dependencies

```bash
pip install aiortc aiohttp av numpy
```

## Files

| File | Description |
|------|-------------|
| `webrtc_server.py` | Main server |
