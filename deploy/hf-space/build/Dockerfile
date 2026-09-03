# Hugging Face Space, Docker SDK.
#
# Streamlit is no longer an SDK the Space create API accepts — it takes only
# gradio|docker|static — so the runtime is pinned here instead, which is better anyway:
# the same CPU-torch discipline as the main image applies, and nothing is inherited from
# a base whose contents can change.
FROM python:3.10-slim-bookworm

# libgomp is the OpenMP runtime faiss-cpu and torch both link against. It ships with the
# build toolchain, so its absence appears only in a slim image, as an ImportError on the
# first `import faiss`.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# HF runs Spaces as uid 1000 and only $HOME is writable. Streamlit, huggingface_hub and
# matplotlib all want a cache directory, and default to paths this user cannot create.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    STREAMLIT_CONFIG_DIR=/home/user/.streamlit \
    PYTHONUNBUFFERED=1

WORKDIR $HOME/app

# torch from the CPU index. On amd64 the PyPI wheel bundles the CUDA runtime — ~2.5 GB of
# nvidia-* this Space can never use, and a Space rebuilds on every push. --no-deps is what
# makes the CPU index usable at all: --index-url replaces PyPI rather than adding to it,
# so torch's ordinary dependencies have nowhere to resolve from. requirements.txt supplies
# them in the next step.
RUN pip install --no-cache-dir --no-deps torch==2.13.0 \
    --index-url https://download.pytorch.org/whl/cpu

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Nothing else would notice the GPU stack reappearing; the Space would just get slower to
# build and larger, which is not a failure anything reports.
RUN leaked="$(pip list --format=freeze | grep -E '^(nvidia-|triton==)' || true)"; \
    if [ -n "$leaked" ]; then echo "CUDA packages on a CPU-only Space:"; echo "$leaked"; exit 1; fi

COPY --chown=user . .

# Model weights are NOT baked. A Space pulls them from the Hub on the same network the Hub
# lives on, so the download is fast and the image stays small; the cost is that the first
# request after a cold start pays it, which the README states plainly.
ENV FINHELM_DATA_DIR=$HOME/app/data \
    FINHELM_DEVICE=cpu \
    OMP_NUM_THREADS=2

EXPOSE 7860

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", "--server.port=7860", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
