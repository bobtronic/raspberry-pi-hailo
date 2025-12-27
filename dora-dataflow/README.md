# Dora Dataflow

Modular dataflow pipeline using [dora-rs](https://dora-rs.ai/) with YAML-configurable rules engine.

## Quick Start

```bash
cd ~/src/raspberry-pi-hailo/dora-dataflow
dora up && dora start dataflow.yaml
```

Open in browser: **http://\<pi-ip\>:8080**

## Features

- **Dora-rs Dataflow**: Modular pipeline with zero-copy Arrow IPC messaging
- **Rules Engine**: YAML-configurable event detection (zones, velocity, loitering)
- **Tracker State**: Enriched detection data with velocity and track history
- **WebRTC Live Stream**: Ultra-low latency video with detection overlays
- **Live Events**: WebSocket broadcast of rule-triggered events

## Architecture

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

## Dataflow Definition

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

## Zero-Copy IPC

Dora uses Apache Arrow for inter-process communication via shared memory:

```python
# Sender: Pass Arrow array directly
node.send_output("detections", pa.array(detection_structs))

# Receiver: Direct access to shared memory
detections = event["value"].to_pylist()
```

## Rules Configuration

Rules are defined in `rules/default.yaml`:

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

### Available Triggers

| Trigger | Description |
|---------|-------------|
| `class` | Filter by object class (person, car, etc.) |
| `zone` | Polygon coordinates (normalized 0-1) |
| `min_duration_sec` | Time in zone before triggering |
| `min_velocity` / `max_velocity` | Speed thresholds |
| `stationary_duration_sec` | Loitering detection |
| `min_age_sec` | Track persistence threshold |

## Resource Usage

CPU percentages are relative to a single core (Pi 5 has 4 cores, so 400% = full utilization).

| Scenario | CPU | Memory |
|----------|-----|--------|
| No viewer | ~94% | ~460 MB |
| With WebRTC viewer | ~173% | ~620 MB |

Process breakdown (idle):

| Node | CPU | Memory | Function |
|------|-----|--------|----------|
| gst-bridge | ~74% | 208 MB | GStreamer + Hailo + WebRTC server |
| tracker-state | ~5% | 71 MB | Track history & velocity |
| rules-engine | ~9% | 71 MB | Rule evaluation |
| rpicam-vid | ~7% | 112 MB | Camera capture |

## Usage

```bash
# Start coordinator and daemon
dora up

# Start pipeline (attached)
dora start dataflow.yaml

# Start pipeline (detached)
dora start dataflow.yaml --detach

# View logs
dora logs <dataflow-id> gst-bridge

# Stop
dora stop

# Shutdown coordinator
dora down
```

## Dependencies

```bash
# Install dora CLI
curl -sSf https://raw.githubusercontent.com/dora-rs/dora/main/install.sh | bash

# Python packages
pip install dora-rs pyarrow aiohttp aiortc av numpy pyyaml
```

## Files

| File | Description |
|------|-------------|
| `dataflow.yaml` | Dora dataflow definition |
| `gst_hailo_pipeline.py` | GStreamer pipeline with Hailo integration |
| `nodes/gst_bridge_webrtc.py` | Bridge node: GStreamer + WebRTC + Dora |
| `nodes/tracker_state.py` | Track history and velocity computation |
| `nodes/rules_engine.py` | YAML-based rule evaluation |
| `nodes/gst_bridge.py` | Bridge node without WebRTC (reference) |
| `rules/default.yaml` | Default rule definitions |
