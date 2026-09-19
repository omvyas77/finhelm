# The Hugging Face Space

The live demo, [huggingface.co/spaces/omvyas77/finhelm](https://huggingface.co/spaces/omvyas77/finhelm),
is a Docker Space built from `build/`. `build/README.md` is the Space's own README; its
front matter is the Space config.

## What runs there

The Streamlit app runs the whole pipeline in-process: routing, question splitting, hybrid
retrieval, reranking and generation, on CPU, with the same served config as the API. There
is no separate backend.

So the Space carries its own corpus. `push_space.sh` copies the filings and complaints FAISS
indexes (209 MB) and the chunk parquet files BM25 reads, since the hybrid retriever falls
back to dense-only without them. The image installs `build/requirements.txt`, a
serving-only set that leaves out the eval harness, MLflow and ragas.

## Secrets and cost

`ANTHROPIC_API_KEY` is set as a Space secret. It is the only credential there: the router,
the question splitter and the answer all call Claude, and the judge never runs on the
Space.

Each question costs about $0.03 on that key. The app caps a browser session at 10
questions (`FINHELM_SESSION_LIMIT` in `build/Dockerfile`). That limits casual use, not a
determined caller, so a spend limit on the Anthropic account is the actual backstop.

## Keeping it in sync

`build/app.py` and `build/src/finhelm/` are copies of the repo's `app.py` and
`src/finhelm/`. After changing either, both of these should print nothing:

```bash
diff app.py deploy/hf-space/build/app.py
diff -r src/finhelm deploy/hf-space/build/src/finhelm
```

`build/evals/history.jsonl` is a copy of `evals/history.jsonl`; the sidebar reads the
frozen run's row from it.

## Deploying

```bash
bash deploy/hf-space/push_space.sh <hf-username>
```

Then add `ANTHROPIC_API_KEY` under the Space's Settings → Variables and secrets.

Two things that cost time the first time. Docker and Streamlit Spaces both need a PRO
subscription; only static Spaces are free. And `short_description` in the front matter must
be 60 characters or fewer, which the Hub checks only after the LFS upload has finished, so
a long one costs a full push before it fails.

## Cold starts

The Space sleeps when idle. Waking it restarts the container, and the first question also
loads the embedding model and the cross-encoder, so it is much slower than the ones after
it. Test the live URL from a phone on cell data after every push: a laptop on the
developer's network is the one client that never reveals a dependency on a local path.
