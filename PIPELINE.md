# GStreamer Pipeline Architecture

## Overview

The `webrtc_server.py` uses a split GStreamer pipeline optimized for low latency and efficient resource usage. The key insight is **tee early at raw I420** to avoid redundant processing.

## Pipeline Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              rpicam-vid                                      │
│                         (YUV420 1280x720 @ 30fps)                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │ rawvideoparse│
                              │   (I420)     │
                              └──────┬──────┘
                                     │
                              ┌──────┴──────┐
                              │  input_tee  │
                              └──────┬──────┘
                                     │
           ┌─────────────────────────┼─────────────────────────┐
           │                         │                         │
           ▼                         ▼                         ▼
   ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
   │   INFERENCE   │         │    WEBRTC     │         │      HLS      │
   │    BRANCH     │         │    BRANCH     │         │    BRANCH     │
   ├───────────────┤         ├───────────────┤         ├───────────────┤
   │               │         │               │         │               │
   │ videoconvert  │         │ videoconvert  │         │ openh264enc   │
   │ (I420 → RGB)  │         │ (I420 → RGB)  │         │ (2.5 Mbps)    │
   │       │       │         │       │       │         │       │       │
   │       ▼       │         │       ▼       │         │       ▼       │
   │  videoscale   │         │   appsink     │         │  h264parse    │
   │  (640x640)    │         │  (RGB frames) │         │       │       │
   │       │       │         │       │       │         │       ▼       │
   │       ▼       │         │       ▼       │         │  hlssink2     │
   │   hailonet    │         │   Python      │         │ (1s segments) │
   │  (YOLOv8s)    │         │   WebRTC      │         │               │
   │       │       │         │   aiortc      │         │  Recordings:  │
   │       ▼       │         │               │         │  /recordings/ │
   │ hailofilter   │         └───────────────┘         └───────────────┘
   │  (NMS post)   │
   │       │       │
   │       ▼       │
   │   appsink     │
   │ (detections)  │
   │       │       │
   │       ▼       │
   │  VTT cues     │
   │  (metadata)   │
   └───────────────┘
```

## Key Design Decisions

### 1. Tee at Raw I420

**Decision:** Split the pipeline immediately after `rawvideoparse`, before any colorspace conversion.

**Why:**
- All branches start from the same frame (perfect sync)
- Each branch only does the conversions it needs
- No redundant processing

**Alternative rejected:** Tee after RGB conversion would mean HLS branch converts RGB→I420 back, wasting CPU.

### 2. Separate Inference and Video Branches

**Decision:** Inference runs on a dedicated branch at 640x640, video branches stay at 1280x720.

**Why:**
- YOLOv8 model expects 640x640 input
- No need to upscale inference output back to 1280x720
- Video quality preserved at full resolution

**Alternative rejected:** Original pipeline scaled down to 640, ran inference, then scaled back up to 1280 - wasteful.

### 3. WebVTT for Detection Sync

**Decision:** Store detection data as WebVTT cues (subtitle format) instead of JSON sidecar.

**Why:**
- Browser's TextTrack API provides automatic sync with video playback
- Same timestamp base as video (no offset needed)
- Seeking works correctly
- Standard format

**Alternative rejected:** JSON sidecar required manual timestamp matching and had sync issues.

### 4. WebRTC via aiortc

**Decision:** Use Python aiortc library for WebRTC instead of GStreamer's webrtcbin.

**Why:**
- Simpler integration with aiohttp web server
- Better control over signaling
- Easier debugging

**Trade-off:** Python copies frames (aiortc re-encodes). Future optimization could use H.264 passthrough.

## Resource Utilization

Measured on Raspberry Pi 5 (8GB) with active WebRTC stream:

| Metric | Value |
|--------|-------|
| **CPU (overall)** | ~33% |
| **python3 CPU** | ~70% (1.7 cores) |
| **python3 RAM** | ~170 MB |
| **rpicam-vid CPU** | ~8% |
| **rpicam-vid RAM** | ~115 MB |
| **Total RAM** | ~490 MB |
| **Temperature** | ~55°C |
| **Hailo-8 NPU** | ~100% during inference |

### CPU Breakdown

| Component | Estimated CPU |
|-----------|---------------|
| GStreamer pipeline | 15% |
| Hailo inference | (offloaded to NPU) |
| aiortc encoding | 40% |
| Python callbacks | 15% |

## Latency Analysis

| Stage | Latency |
|-------|---------|
| Camera capture | ~33ms (1 frame @ 30fps) |
| Hailo inference | ~30ms |
| WebRTC encoding | ~20ms |
| Network + decode | ~50ms |
| **Total (live)** | **~150ms** |

HLS recording adds ~1-2s due to segment buffering, but this doesn't affect live view.

## Recording Format

Each recording in `/home/bob/recordings/<timestamp>/` contains:

| File | Description |
|------|-------------|
| `playlist.m3u8` | HLS VOD playlist |
| `segment*.ts` | 1-second H.264 segments |
| `detections.vtt` | WebVTT metadata with JSON detection payloads |

### VTT Format Example

```vtt
WEBVTT
Kind: metadata

00:00:01.234 --> 00:00:01.284
[{"label":"person","confidence":0.85,"bbox":{"x":0.1,"y":0.2,"w":0.3,"h":0.5},"track_id":1}]

00:00:01.267 --> 00:00:01.317
[{"label":"person","confidence":0.86,"bbox":{"x":0.11,"y":0.21,"w":0.3,"h":0.5},"track_id":1}]
```

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

## Future Optimizations

1. **H.264 passthrough for WebRTC**: Avoid Python re-encoding by passing H.264 NAL units directly to aiortc
2. **Hardware H.264 encoder**: Use Pi's hardware encoder instead of openh264enc
3. **Reduced resolution for WebRTC**: Could use 720p or 480p for lower bandwidth
4. **Adaptive bitrate**: Adjust encoding based on network conditions
