#!/bin/sh
# Start the worker in a container. Needs only SSW_WORKER_KEY (the worker key from the owner).
# Optional: SSW_MACHINE_NAME, SSW_API_BASE, SSW_DATA (a mounted disk for model files and work files).
set -eu

if [ -z "${SSW_WORKER_KEY:-}" ]; then
    echo "SSW_WORKER_KEY is not set. Start the container with -e SSW_WORKER_KEY=sswk_..." >&2
    exit 2
fi

HOME_DIR="${SSW_HOME:-/opt/ssw}"

# A mounted disk, when there is one, holds the work files and (for the slim image) the model files, so the next
# machine that mounts it starts without downloading. /workspace is where rented GPU hosts usually mount theirs.
if [ -n "${SSW_DATA:-}" ]; then DATA="$SSW_DATA"
elif [ -d /data ]; then DATA=/data
elif [ -d /workspace ]; then DATA=/workspace/speakstone
else DATA="$HOME_DIR/data"
fi
mkdir -p "$DATA"

# Model files: inside the image when it was built with them, otherwise on the data disk (fetched once).
if python -c "import sys; sys.path.insert(0, '$HOME_DIR'); import fetch_models as f; sys.exit(0 if f.complete('$HOME_DIR/models') else 1)"; then
    MODELS="$HOME_DIR/models"
else
    MODELS="$DATA/models"
    python "$HOME_DIR/fetch_models.py" "$MODELS"
    # converted once on the data disk, which outlives the machine; the worker skips a model already converted
fi

export INDEXTTS2_DIR="$MODELS/IndexTTS-2"
export INDEXTTS2_PYTHON="$(command -v python)"
export HF_HOME="$MODELS/hf"
export HF_HUB_CACHE="$MODELS/hf/hub"
export SSW_WORK_DIR="${SSW_WORK_DIR:-$DATA/work}"
export HF_HUB_ENABLE_HF_TRANSFER=0

nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null \
    || echo "warning: nvidia-smi found no graphics card; start the container with --gpus all" >&2

# exec: the worker gets the container's stop signal directly, so it can give its job back before it exits
exec python "$HOME_DIR/net_worker.py" run "$@"
