"""Fetch the model weights into the image, then prove they load with the network fenced.

A script rather than a heredoc in the Dockerfile, and the reason is not style. `RUN python
- <<'PY'` is a BuildKit feature. Run under the legacy builder — which is what you get when
`docker buildx` is not installed, with no warning that it happened — the heredoc has no
body, python reads an empty stdin, and the step **exits 0 having done nothing**. Both
weight steps silently no-opped and the build failed three stages later at a COPY, pointing
at the wrong thing entirely. A missing file fails loudly; a no-op does not.

Two subcommands because the offline fence goes on between them, in the Dockerfile, using
the same environment variables the runtime image sets:

    python scripts/bake_weights.py fetch
    ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
    python scripts/bake_weights.py verify --config src/finhelm/config.py
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# These repos also carry ONNX exports and a duplicate pytorch_model.bin, none of which
# sentence-transformers loads when safetensors are present. A bare snapshot would roughly
# double the layer for no runtime gain.
ALLOW_PATTERNS = ["*.json", "*.txt", "*.safetensors", "1_Pooling/*"]


def _models() -> tuple[str, str]:
    try:
        return os.environ["EMBED_MODEL"], os.environ["RERANK_MODEL"]
    except KeyError as exc:
        raise SystemExit(f"{exc.args[0]} is not set; the Dockerfile promotes the ARGs "
                         f"to ENV so this script can read them") from exc


def fetch() -> None:
    from huggingface_hub import snapshot_download

    for repo in _models():
        path = snapshot_download(repo, allow_patterns=ALLOW_PATTERNS)
        files = sorted(p.name for p in Path(path).rglob("*") if p.is_file())
        # Printed, and asserted: an empty snapshot is the failure this file exists to
        # make impossible to miss.
        if not any(f.endswith(".safetensors") for f in files):
            raise SystemExit(f"{repo} fetched no safetensors — got {files}")
        print(f"fetched {repo} -> {path} ({len(files)} files)")


def verify(config_path: str) -> None:
    from sentence_transformers import CrossEncoder, SentenceTransformer

    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise SystemExit("verify must run with HF_HUB_OFFLINE=1, or it proves nothing: "
                         "a missing file would be downloaded instead of reported")

    embed_model, rerank_model = _models()
    embed = SentenceTransformer(embed_model)
    rerank = CrossEncoder(rerank_model)

    spec = importlib.util.spec_from_file_location("finhelm_config", config_path)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)

    # The check this step exists to make. EMBED_DIMS is what a store uses to size a vector
    # column before a single embedding exists, so nothing at runtime ever compares it
    # against the model — a 384 sitting beside a 768-dim model stayed invisible until
    # Day 3.4. Asserting it here means the image cannot ship with the two disagreeing.
    declared = config.EMBED_DIMS[embed_model]
    # Renamed in sentence-transformers 6; the old name warns but still works.
    actual = (embed.get_embedding_dimension()
              if hasattr(embed, "get_embedding_dimension")
              else embed.get_sentence_embedding_dimension())
    if declared != actual:
        raise SystemExit(f"EMBED_DIMS declares {declared} for {embed_model}, "
                         f"the model emits {actual}")

    score = rerank.predict([("a question", "a passage")])
    print(f"offline load OK: {embed_model} at {actual} dims "
          f"(EMBED_DIMS agrees), reranker scored {float(score[0]):.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    verifier = sub.add_parser("verify")
    verifier.add_argument("--config", required=True,
                          help="path to finhelm's config.py; it imports only dataclasses")

    args = parser.parse_args()
    if args.command == "fetch":
        fetch()
    else:
        verify(args.config)


if __name__ == "__main__":
    sys.exit(main())
