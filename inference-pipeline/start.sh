#!/bin/bash
# Start the Dora inference pipeline

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Source dora CLI environment
if [ -f "$HOME/.dora/bin/env" ]; then
    source "$HOME/.dora/bin/env"
fi

# Check for dora CLI
if ! command -v dora &> /dev/null; then
    echo "Error: dora CLI not found."
    echo "Install with: curl --proto '=https' --tlsv1.2 -LsSf https://github.com/dora-rs/dora/releases/latest/download/dora-cli-installer.sh | sh"
    exit 1
fi

# Check for required Python packages
python3 -c "import pyarrow" 2>/dev/null || {
    echo "Error: pyarrow not found. Install with: pip install pyarrow"
    exit 1
}

python3 -c "import dora" 2>/dev/null || {
    echo "Error: dora-rs not found. Install with: pip install dora-rs"
    exit 1
}

python3 -c "import yaml" 2>/dev/null || {
    echo "Error: pyyaml not found. Install with: pip install pyyaml"
    exit 1
}

# Check for GStreamer bindings
python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst" 2>/dev/null || {
    echo "Error: GStreamer Python bindings not found."
    echo "Install with: sudo apt install python3-gi gstreamer1.0-tools"
    exit 1
}

# Optional: Check for hailo
python3 -c "import hailo" 2>/dev/null || {
    echo "Warning: hailo module not found. Pipeline may not work without Hailo SDK."
}

echo "Starting Dora dataflow..."
echo "Press Ctrl+C to stop"
echo ""

# Start the dataflow
# Use 'dora run' for local execution without coordinator
dora run dataflow.yaml
