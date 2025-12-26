# Architecture: GStreamer + Hailo + Dora Integration

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         GStreamer Pipeline                                   │
│                                                                              │
│  rpicam-vid → videoconvert → hailonet → hailofilter → hailotracker         │
│                                              │              │                │
│                                              │              ▼                │
│                                              │      hailooverlay → display   │
│                                              │                               │
│                                              ▼                               │
│                                      identity callback ──────────────────────┤
└──────────────────────────────────────────────│───────────────────────────────┘
                                               │
                           ┌───────────────────▼───────────────────┐
                           │        Dora Dataflow                  │
                           │                                       │
                           │  ┌─────────────┐    ┌──────────────┐  │
                           │  │ gst-bridge  │───▶│ tracker-state│  │
                           │  │   (Python)  │    │   (Python)   │  │
                           │  └─────────────┘    └──────┬───────┘  │
                           │         │                  │          │
                           │         │ (on-demand       ▼          │
                           │         │  frames)   ┌──────────────┐ │
                           │         │            │ rules-engine │ │
                           │         ▼            │   (Python)   │ │
                           │  ┌─────────────┐     └──────┬───────┘ │
                           │  │  cv-proc    │            │         │
                           │  │ (optional)  │            ▼         │
                           │  └─────────────┘     ┌──────────────┐ │
                           │                      │   outputs    │ │
                           │                      │ (actions/log)│ │
                           │                      └──────────────┘ │
                           └───────────────────────────────────────┘
```

## Components

### 1. GStreamer Pipeline (`gst_hailo_pipeline.py`)

**Purpose:** Camera capture, Hailo inference, tracking, and optional display

**Pipeline elements:**
```
rpicam-vid (YUV420)
  → rawvideoparse
  → videoconvert (RGB)
  → videoscale (640x640 for model)
  → hailonet (yolov8s_h8.hef)
  → hailofilter (libyolo_hailortpp_post.so)
  → hailotracker (IoU-based tracking with track IDs)
  → tee name=t
      t. → queue → hailooverlay → autovideosink (optional display)
      t. → queue → identity name=dora_tap signal-handoffs=true → fakesink
```

**Key features:**
- `hailotracker` provides persistent track IDs across frames
- `identity` element taps the stream for Python callback
- Callback extracts HailoROI metadata (boxes, classes, scores, track IDs)
- Sends detection JSON to Dora via shared memory or ZMQ

**File:** `gst_hailo_pipeline.py`

---

### 2. GStreamer-Dora Bridge Node (`nodes/gst_bridge.py`)

**Purpose:** Dora node that hosts GStreamer pipeline and publishes detections

**Outputs:**
- `detections`: Arrow RecordBatch with columns:
  - `frame_id: u64`
  - `timestamp_ms: f64`
  - `track_id: i32`
  - `class_id: u8`
  - `class_name: string`
  - `confidence: f32`
  - `x, y, w, h: f32` (normalized 0-1)

- `frame` (on-demand): Raw frame bytes when requested

**Inputs:**
- `frame_request`: Trigger to capture next frame

**Implementation:**
```python
from dora import Node
import pyarrow as pa

class GstBridgeNode:
    def __init__(self):
        self.node = Node()
        self.pipeline = GstHailoPipeline()
        self.capture_next_frame = False

    def on_detection(self, detections, frame=None):
        # Convert to Arrow RecordBatch
        batch = pa.RecordBatch.from_pydict({...})
        self.node.send_output("detections", batch.serialize())

        if self.capture_next_frame and frame is not None:
            self.node.send_output("frame", frame)
            self.capture_next_frame = False
```

**File:** `nodes/gst_bridge.py`

---

### 3. Tracker State Node (`nodes/tracker_state.py`)

**Purpose:** Maintain object tracking state over time for rules engine

**Inputs:**
- `detections`: Per-frame detection batch from gst_bridge

**Outputs:**
- `tracks`: Enriched track data with history
  - `track_id`
  - `age_frames: u32` (how long tracked)
  - `positions: list[{x,y,w,h,t}]` (recent position history)
  - `velocity: {vx, vy}` (estimated motion)
  - `class_id`, `class_name`
  - `avg_confidence: f32`

**State maintained:**
- Active tracks (seen in last N frames)
- Track history (positions, sizes over time)
- Track lifecycle events (new, lost, recovered)

**File:** `nodes/tracker_state.py`

---

### 4. Rules Engine Node (`nodes/rules_engine.py`)

**Purpose:** Higher-level inferences based on tracking data

**Inputs:**
- `tracks`: Enriched track data from tracker_state

**Outputs:**
- `events`: High-level events
  - `person_entered_zone`
  - `object_stationary_too_long`
  - `unusual_motion_pattern`
  - etc.
- `actions`: Commands for downstream actuators

**Configuration:** YAML-based rules definition
```yaml
rules:
  - name: person_in_zone
    trigger:
      class: person
      zone: [[0.1,0.1], [0.5,0.1], [0.5,0.9], [0.1,0.9]]
      min_duration_sec: 2.0
    action:
      type: log
      message: "Person detected in restricted zone"
```

**File:** `nodes/rules_engine.py`

---

### 5. Dora Dataflow Definition (`dataflow.yaml`)

```yaml
nodes:
  - id: gst-bridge
    custom:
      source: python
      module: nodes.gst_bridge
    inputs:
      frame_request: rules-engine/frame_request
    outputs:
      - detections
      - frame

  - id: tracker-state
    custom:
      source: python
      module: nodes.tracker_state
    inputs:
      detections: gst-bridge/detections
    outputs:
      - tracks

  - id: rules-engine
    custom:
      source: python
      module: nodes.rules_engine
    inputs:
      tracks: tracker-state/tracks
    outputs:
      - events
      - actions
      - frame_request

  - id: rerun-viz  # Optional visualization
    custom:
      source: python
      module: nodes.rerun_viz
    inputs:
      tracks: tracker-state/tracks
      frame: gst-bridge/frame
```

---

## Data Flow

1. **GStreamer Pipeline** runs continuously at 30 FPS
2. **hailotracker** assigns persistent track IDs to detections
3. **identity callback** extracts metadata, sends to Dora
4. **gst-bridge** publishes Arrow-serialized detections
5. **tracker-state** enriches with temporal history
6. **rules-engine** evaluates rules, emits events/actions
7. **On-demand frames**: rules-engine can request frames when needed

## File Structure

```
inference-pipeline/
├── ARCHITECTURE.md           # This file
├── README.md                 # Setup and usage instructions
├── dataflow.yaml             # Dora graph definition
├── gst_hailo_pipeline.py     # GStreamer pipeline class
├── nodes/
│   ├── __init__.py
│   ├── gst_bridge.py         # GStreamer ↔ Dora bridge
│   ├── tracker_state.py      # Track history management
│   ├── rules_engine.py       # Rule evaluation
│   └── rerun_viz.py          # Optional Rerun visualization
├── rules/
│   └── default.yaml          # Rule definitions
└── start.sh                  # Launch script (dora up dataflow.yaml)
```

## Implementation Order

1. `gst_hailo_pipeline.py` - GStreamer pipeline with identity callback
2. `nodes/gst_bridge.py` - Dora node wrapping pipeline
3. `dataflow.yaml` - Basic dataflow with just gst-bridge
4. `nodes/tracker_state.py` - Track history enrichment
5. `nodes/rules_engine.py` - Rule evaluation
6. `rules/default.yaml` - Initial rule definitions
7. `start.sh` - Launch script
8. `nodes/rerun_viz.py` - Optional visualization

## Dependencies

**Python packages:**
- `dora-rs` - Dora runtime
- `pyarrow` - Arrow serialization
- `PyGObject` - GStreamer bindings (already present)
- `hailo_platform` - Hailo SDK (already present)

**System:**
- `hailo-tappas-core` - hailotracker, hailofilter elements
- `dora` CLI - Dora runtime

## Key Technical Decisions

1. **Arrow format** for zero-copy detection data between nodes
2. **hailotracker in GStreamer** (not in Dora) - uses optimized C++ implementation
3. **On-demand frame capture** via request/response pattern
4. **Python nodes** for flexibility in rules engine logic
5. **Separate tracker-state node** to isolate temporal logic from bridge

## References

- [Hailo RPi5 Examples](https://github.com/hailo-ai/hailo-rpi5-examples)
- [Hailo TAPPAS](https://github.com/hailo-ai/tappas)
- [dora-rs](https://github.com/dora-rs/dora)
- [Extracting Inference Results from TAPPAS Pipeline](https://community.hailo.ai/t/extracting-inference-results-from-a-tappas-pipeline-in-python/22)
