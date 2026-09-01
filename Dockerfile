# syntax=docker/dockerfile:1.7
#
# Two stages. The builder carries a C toolchain and pip's ~1 GB of wheel downloads; the
# runtime carries neither, because the only thing worth shipping out of a build is the
# result. Model weights are baked in a dedicated layer — see the offline note below.

ARG PYTHON_VERSION=3.10-slim-bookworm
# Pinned to what actually produced the measured numbers. `python:3-slim` would silently
# move to 3.13 on a rebuild and change resolved wheels underneath a frozen requirements
# file, which is how a reproducible image stops being one.

# ---------------------------------------------------------------- builder ---
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# torch from PyTorch's CPU index, with --no-deps, on every architecture.
#
# Three findings, each of which cost a build to establish:
#
#   * PyPI's linux torch wheel is a CUDA build on arm64 as well as amd64 — this one is
#     literally `2.13.0+cu130`. It does not merely carry 2.1 GB of nvidia-cublas, cudnn,
#     nccl and triton as dependencies; it hard-links libcudart at import, so installing
#     it without them yields a torch that cannot `import torch` at all.
#   * The CPU index does publish `torch-2.13.0+cpu-cp310-manylinux_2_28_aarch64.whl`.
#     The earlier "could not find flit_core" failure was never about torch: --index-url
#     *replaces* PyPI rather than adding to it, so torch's ordinary Python dependencies
#     had no wheels to resolve from and pip fell back to building them from sdists.
#   * --no-deps is therefore what makes the CPU index usable, not a size optimisation.
#     requirements.txt supplies those dependencies from PyPI in the next step.
#
# And the reason the requirements install below can go back to resolving normally: pip
# uses the *installed* distribution's metadata for a requirement it already satisfies, and
# the +cpu wheel declares no nvidia-* dependencies. With the +cu130 wheel installed, the
# very same `pip install -r requirements.txt` printed "Requirement already satisfied:
# torch" and then downloaded 542 MB of nvidia-cublas.
ARG TORCH_VERSION=2.13.0
RUN pip install --no-deps "torch==${TORCH_VERSION}" \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Split from the leak check below: one `&&`/`||` chain covering both would report
# "CUDA packages leaked" when what actually happened was that torch failed to import —
# which is exactly the failure that caught the +cu130 wheel.
RUN python -c "import torch; print('torch', torch.__version__)"
RUN leaked="$(pip list --format=freeze | grep -E '^(nvidia-|triton==|pytorch-triton)' || true)"; \
    if [ -n "$leaked" ]; then \
        echo "CUDA packages leaked into a CPU-only image:"; \
        echo "$leaked"; \
        exit 1; \
    fi

# --------------------------------------------------------------- weights ---
# Baked, not fetched at boot. Pulling bge-base and the reranker on first request costs
# ~60 s of cold start and makes the container's readiness depend on huggingface.co being
# up — an external dependency in the request path of a service that otherwise has none.
#
# Its own stage so that editing requirements.txt does not re-download 1.5 GB of weights,
# and editing the model pins does not rebuild the dependency tree.
FROM builder AS weights

ARG EMBED_MODEL=BAAI/bge-base-en-v1.5
ARG RERANK_MODEL=BAAI/bge-reranker-base

# Promoted from ARG to ENV so bake_weights.py can read them, and so the runtime stage
# below is configured from the same two values rather than a second copy of the strings.
ENV HF_HOME=/opt/hf
ENV EMBED_MODEL=${EMBED_MODEL}
ENV RERANK_MODEL=${RERANK_MODEL}

# Two steps, with the offline fence switched on between them, and both delegating to a
# real script. Heredocs (`RUN python - <<'PY'`) are a BuildKit feature: under the legacy
# builder — which is what `docker build` silently falls back to when `docker buildx` is not
# installed — the heredoc has no body, python reads an empty stdin, and the step exits 0
# having done nothing. That is how the first version of this file "succeeded" all the way
# to a COPY three stages later. See scripts/bake_weights.py.
COPY scripts/bake_weights.py /tmp/bake_weights.py
RUN python /tmp/bake_weights.py fetch

# config.py alone, not the package: it imports nothing but `dataclasses`, so the declared
# embedding width can be checked here without dragging the application into this stage.
COPY src/finhelm/config.py /tmp/config.py

# The fence goes on before the check, not after, or the check proves nothing — a file the
# fetch missed would simply be downloaded rather than reported. These are the same two
# variables the runtime stage sets, so this verifies the runtime's actual conditions.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
RUN python /tmp/bake_weights.py verify --config /tmp/config.py

# --------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION} AS runtime

# libgomp is the OpenMP runtime both faiss-cpu and torch link against. It ships with the
# build toolchain, so its absence surfaces only in the slim runtime — as an ImportError on
# the first `import faiss`, long after the build reported success.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# A fixed high uid, not just "not root": it stays out of the range a host distro assigns
# to real accounts, so a bind-mounted volume can be owned by it deliberately.
RUN groupadd --gid 10001 finhelm \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin finhelm

COPY --from=builder /opt/venv /opt/venv
COPY --from=weights /opt/hf /opt/hf

ENV PATH="/opt/venv/bin:$PATH"
ENV HF_HOME=/opt/hf
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Fenced deliberately. Every weight the service loads is in the image; if something tries
# to reach the Hub at runtime that is a bug, and failing loudly beats a silent 60-second
# download sitting in a request path.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# src layout, and the package is not pip-installed into the venv on purpose: a code change
# then invalidates only the COPY below instead of the dependency layer above it.
ENV PYTHONPATH=/app/src

WORKDIR /app

# Ordered by how often each changes. src/ last means editing a retriever rebuilds one
# small layer rather than re-copying the eval harness and the golden set.
COPY --chown=finhelm:finhelm pyproject.toml ./
COPY --chown=finhelm:finhelm evals/ ./evals/
COPY --chown=finhelm:finhelm scripts/ ./scripts/
COPY --chown=finhelm:finhelm app.py ./
COPY --chown=finhelm:finhelm src/ ./src/

# data/ is a mount point, not content: 961 MB of FAISS indexes that are rebuildable, that
# change far more often than the code, and that would make every `docker push` move a
# gigabyte. Created here so the directory exists and is writable when nothing is mounted.
RUN mkdir -p /app/data && chown finhelm:finhelm /app/data

USER finhelm

# The whole application graph, imported as the non-root user with the Hub fenced off.
# torch arrives here via --no-deps from a separate index, so this is what confirms the
# requirements install actually put back everything torch needs — and it covers what the
# weights stage cannot: faiss's OpenMP linkage, streamlit, the FastAPI app, and whether
# uid 10001 can read the files COPY placed. Model loading stays lazy, so it costs about a
# second and downloads nothing.
RUN python -c "import faiss, pandas, rank_bm25, streamlit, uvicorn; \
import finhelm.api, finhelm.generate, finhelm.retrieve, finhelm.telemetry; \
print('import closure OK:', finhelm.api.CONFIG.run_name())"

EXPOSE 8000

# Deliberately stricter than "the process is listening". /health reports `degraded` when
# the FAISS index is absent, which is exactly the state a forgotten volume mount produces:
# a container that answers every request with an abstention and looks fine doing it.
# Gating on `ok` turns that into an unhealthy container instead of a silently useless one.
# start-period covers model load, which is ~20 s on a cold container.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "\
import json, sys, urllib.request; \
r = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)); \
sys.exit(0 if r.get('status') == 'ok' else 1)"

CMD ["uvicorn", "finhelm.api:app", "--host", "0.0.0.0", "--port", "8000"]
