#!/usr/bin/env bash
# Create the Space and push it. Run from the repo root.
#
#   huggingface-cli login          # once, needs a WRITE token from
#                                  # https://huggingface.co/settings/tokens
#   bash deploy/hf-space/push_space.sh <your-hf-username>
#
# Then add ANTHROPIC_API_KEY as a Space *secret* in the Space's Settings page. It is the
# only credential the Space needs — generation is the only thing that calls a model, and
# the judge never runs here.
set -euo pipefail

USER="${1:?usage: push_space.sh <hf-username>}"
SPACE="$USER/finhelm"
BUILD="deploy/hf-space/build"
STAGE="$(mktemp -d)/finhelm"

command -v git-lfs >/dev/null || { echo "git-lfs required: brew install git-lfs"; exit 1; }

# streamlit is no longer an accepted SDK; the runtime is pinned by the Dockerfile.
huggingface-cli repo create finhelm --type space --space_sdk docker -y || true
git clone "https://huggingface.co/spaces/$SPACE" "$STAGE"

cp -R "$BUILD"/. "$STAGE"/
mkdir -p "$STAGE/data/index"
# 209 MB of FAISS index. The Space has no API backend to retrieve from, so it carries the
# corpus itself; LFS is what makes that pushable.
cp -R data/index/filings_semantic_ctx_bge-base-en-v15 "$STAGE/data/index/"
cp -R data/index/complaints_fixed_ctx_bge-base-en-v15 "$STAGE/data/index/"
mkdir -p "$STAGE/data/processed"
# BM25 reads the chunk parquet; the hybrid retriever needs both halves or it silently
# falls back to dense-only and the measured numbers stop applying.
cp data/processed/chunks_filings_semantic.parquet "$STAGE/data/processed/"
cp data/processed/chunks_complaints_fixed.parquet "$STAGE/data/processed/"

cd "$STAGE"
git lfs install
git lfs track "*.faiss" "*.jsonl" "*.parquet"
git add -A
git -c user.email="$(git -C - config user.email 2>/dev/null || echo noreply@huggingface.co)" \
    -c user.name="finhelm" commit -q -m "finhelm: Streamlit demo with the served config and its index"
git push
echo
echo "Space: https://huggingface.co/spaces/$SPACE"
echo "Now add ANTHROPIC_API_KEY under Settings -> Variables and secrets."
