# Hailo Inference Pipeline with Dora

A modular inference pipeline using GStreamer, Hailo-8 NPU, and Dora dataflow framework.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         GStreamer Pipeline                               │
│                                                                          │
│  rpicam-vid → videoconvert → hailonet → hailofilter → hailotracker      │
│                                                            │             │
│                                               identity callback ─────────┤
└──────────────────────────────────────────────│───────────────────────────┘
                                               │
                         ┌─────────────────────▼─────────────────────┐
                         │            Dora Dataflow                  │
                         │                                           │
                         │  ┌─────────────┐    ┌──────────────┐     │
                         │  │ gst-bridge  │───▶│ tracker-state│     │
                         │  │  (Python)   │    │   (Python)   │     │
                         │  └─────────────┘    └──────┬───────┘     │
                         │                            │              │
                         │                            ▼              │
                         │                     ┌──────────────┐     │
                         │                     │ rules-engine │     │
                         │                     │   (Python)   │     │
                         │                     └──────┬───────┘     │
                         │                            │              │
                         │                            ▼              │
                         │                      events/actions       │
                         └───────────────────────────────────────────┘
```

## Components

| Node | Purpose |
|------|---------|
| **gst-bridge** | Wraps GStreamer/Hailo pipeline, publishes detections |
| **tracker-state** | Maintains track history, computes velocity |
| **rules-engine** | Evaluates rules, emits events/actions |

## Requirements

```bash
# Python packages
pip install dora-rs pyarrow pyyaml

# System (on Raspberry Pi)
# - hailo-tappas-core (provides hailotracker, hailofilter)
# - hailo-rt (Hailo runtime)
```

## Usage

```bash
# Start the pipeline
./start.sh

# Or manually with dora
dora up dataflow.yaml

# Stop
dora destroy
```

## Configuration

### Camera Settings

Set environment variables or edit `dataflow.yaml`:

```yaml
env:
  CAM_WIDTH: "1280"
  CAM_HEIGHT: "720"
  CAM_FPS: "30"
```

### Rules

Edit `rules/default.yaml` to define detection rules:

```yaml
rules:
  - name: person_in_zone
    trigger:
      class: person
      zone: [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]
      min_duration_sec: 2.0
    action:
      type: log
      message: "Person detected in zone"
    cooldown_sec: 10.0
```

### Trigger Types

| Trigger | Parameters |
|---------|------------|
| **class** | Filter by object class (person, car, etc.) |
| **zone** | Polygon coordinates [[x,y], ...] (normalized 0-1) |
| **min_duration_sec** | Minimum time in zone |
| **min_velocity** / **max_velocity** | Speed thresholds |
| **stationary_duration_sec** | Time without movement |
| **min_age_sec** | Minimum track age |

### Action Types

| Action | Description |
|--------|-------------|
| **log** | Print message to console |
| **capture_frame** | Request frame capture |
| **webhook** | POST to URL (not yet implemented) |

## Data Format

Detection data uses Apache Arrow for efficient serialization:

```python
schema = pa.schema([
    ('frame_id', pa.uint64()),
    ('timestamp_ms', pa.float64()),
    ('track_id', pa.int32()),
    ('class_name', pa.string()),
    ('confidence', pa.float32()),
    ('x', pa.float32()),
    ('y', pa.float32()),
    ('w', pa.float32()),
    ('h', pa.float32()),
])
```

## Testing Without Hardware

Test individual nodes:

```bash
# Test gst_hailo_pipeline (needs camera + Hailo)
python3 gst_hailo_pipeline.py

# Test rules engine parsing
python3 -c "from nodes.rules_engine import RulesEngineNode; r = RulesEngineNode()"
```

## Files

```
inference-pipeline/
├── ARCHITECTURE.md       # Detailed architecture doc
├── README.md             # This file
├── dataflow.yaml         # Dora graph definition
├── gst_hailo_pipeline.py # GStreamer pipeline class
├── start.sh              # Launch script
├── nodes/
│   ├── __init__.py
│   ├── gst_bridge.py     # GStreamer ↔ Dora bridge
│   ├── tracker_state.py  # Track history management
│   └── rules_engine.py   # Rule evaluation
└── rules/
    └── default.yaml      # Rule definitions
```
