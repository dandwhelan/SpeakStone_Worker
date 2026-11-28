# SpeakStone Worker as a container for rented cloud GPUs (Linux, NVIDIA).
#
# Everything a worker needs is put in the image at build time, so a new machine only pulls the image and starts:
# no setup step, no downloads at start. Build from the repository root:
#
#   docker build -f docker/Dockerfile -t speakstone-worker .                         # full image, model files inside
#   docker build -f docker/Dockerfile --build-arg BAKE_MODELS=0 -t speakstone-worker:slim .
#
# The slim image leaves the model files out (about 6 GB smaller). It fetches them on first start into /data, so
# mount a disk that outlives the machine there (a network volume) and later machines start without downloading.
#
# Run:  docker run --gpus all -e SSW_WORKER_KEY=sswk_... speakstone-worker
#
# CUDA itself comes inside the torch wheels; the host only needs the NVIDIA driver and container runtime, which
# rented GPU hosts provide. That is why the base is plain Python and not a CUDA image (several GB smaller).
FROM python:3.11-slim-bookworm

ARG INDEX_TTS_COMMIT=830f6f8
ARG TORCH_VERSION=2.8.0+cu128
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
ARG BAKE_MODELS=1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    SSW_HOME=/opt/ssw \
    HF_HUB_ENABLE_HF_TRANSFER=0

# ffmpeg levels and encodes every clip. The compiler is only needed while packages install and is removed again in
# the same layer, so it adds nothing to the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip build-essential \
 && pip install uv \
 && printf 'torch==%s\ntorchaudio==%s\n' "$TORCH_VERSION" "$TORCH_VERSION" > /opt/constraints.txt \
 # torch first, from its own index; every later install is held to it by the constraints file
 && uv pip install --system -c /opt/constraints.txt --index-url "$TORCH_INDEX" "torch==$TORCH_VERSION" "torchaudio==$TORCH_VERSION" \
 && curl -fsSL "https://github.com/index-tts/index-tts/archive/${INDEX_TTS_COMMIT}.zip" -o /tmp/it.zip \
 && unzip -q /tmp/it.zip -d /tmp/it && mkdir -p "$SSW_HOME" && mv /tmp/it/index-tts-* "$SSW_HOME/index-tts" \
 && uv pip install --system -c /opt/constraints.txt -e "$SSW_HOME/index-tts" soundfile numpy huggingface_hub \
 && uv pip install --system -c /opt/constraints.txt --upgrade "omegaconf>=2.3" "protobuf<3.21" \
 # triton (about 540 MB) only backs torch.compile, which the engine is never asked for; torch imports fine without it
 && uv pip uninstall --system triton \
 && python -c "import torch, sys; v = torch.__version__; print('torch', v); sys.exit(0 if v == '$TORCH_VERSION' else 1)" \
 && apt-get purge -y build-essential unzip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /tmp/it /tmp/it.zip /root/.cache

COPY docker/fetch_models.py docker/entrypoint.sh /opt/ssw/
COPY net_worker.py indextts2_worker.py runpod_worker.py /opt/ssw/

# Model files (about 3 GB once gpt.pth is stored as float16) and the four extra voice files (about 3 GB). The unused
# text model is left out. Converting here, once, means a fresh container never spends its first minutes rewriting
# 1.7 GB (the worker would otherwise do it at every start, since a new container starts from the image again).
RUN chmod +x /opt/ssw/entrypoint.sh \
 && if [ "$BAKE_MODELS" = "1" ]; then python /opt/ssw/fetch_models.py /opt/ssw/models \
    && python /opt/ssw/indextts2_worker.py --shrink-model /opt/ssw/models/IndexTTS-2 \
    && test -f /opt/ssw/models/IndexTTS-2/.fp16.json; fi

WORKDIR /opt/ssw
ENTRYPOINT ["/opt/ssw/entrypoint.sh"]
