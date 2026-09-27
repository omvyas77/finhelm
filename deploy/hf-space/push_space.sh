#!/usr/bin/env bash
# Create the Space and push it. Run from the repo root.
#
#   hf auth login          # once, with a WRITE token from
#                          # https://huggingface.co/settings/tokens
#   bash deploy/hf-space/push_space.sh <your-hf-username>
#
# Then add ANTHROPIC_API_KEY as a Space *secret* in the Space's Settings page. It is the
# only credential the Space needs, generation is the only thing that calls a model, and
# the judge never runs here.
set -euo pipefail

USER="${1:?usage: push_space.sh <hf-username>}"
SPACE="$USER/finhelm"
BUILD="deploy/hf-space/build"
STAGE="$(mktemp -d)/finhelm"

command -v git-lfs >/dev/null || { echo "git-lfs required: brew install git-lfs"; exit 1; }
command -v hf >/dev/null || { echo "hf CLI required: pip install -U huggingface_hub"; exit 1; }

# docker, not streamlit: the streamlit SDK no longer accepts this app's dependency set,
# so the runtime is pinned by build/Dockerfile instead. Already-exists is not an error.
hf repo create "$SPACE" --repo-type space --space_sdk docker -y || true
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
# The Space repo has no identity configured; borrow the caller's, or fall back.
EMAIL="$(git config --global user.email || echo noreply@huggingface.co)"
NAME="$(git config --global user.name || echo finhelm)"
git -c user.email="$EMAIL" -c user.name="$NAME" \
    commit -q -m "Deploy the demo with the served config and its index"
git push
echo
echo "Space: https://huggingface.co/spaces/$SPACE"
echo "Now add ANTHROPIC_API_KEY under Settings -> Variables and secrets."
