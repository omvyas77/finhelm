# finhelm

[![eval-gate](https://github.com/omvyas77/finhelm/actions/workflows/eval-gate.yml/badge.svg?branch=main)](https://github.com/omvyas77/finhelm/actions/workflows/eval-gate.yml)
[![live demo](https://img.shields.io/badge/demo-Hugging%20Face%20Space-blue)](https://huggingface.co/spaces/omvyas77/finhelm)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Ask questions about US bank SEC filings. Get answers with every claim cited to the
filing it came from — or a refusal when the filings don't say.**

**[Try it →](https://huggingface.co/spaces/omvyas77/finhelm)**  The first question after
idle takes about a minute while the demo wakes up.

| Finds the evidence | Citations valid | Refuses when it should | Cost per question |
|:--:|:--:|:--:|:--:|
| **74%** | **100%** | **90%** | **$0.03** |

Measured on 202 questions written and checked by hand, 21 of which have no answer in the
corpus at all.

---

## Why this exists

A credit analyst wants to know how Capital One and Synchrony each describe credit
normalization in their latest 10-Ks. SQL can't answer that — the fact is a paragraph of
Item 7, worded differently by each filer. Full-text search returns the right document and
leaves you forty pages to read. A chatbot answers fluently and sometimes invents the
number.

This retrieves the specific passages, answers only from them, cites each claim to a
document you can open, and says "I don't have that" when the evidence isn't there.

The refusal is the part that makes it usable in finance, so it's measured as carefully as
the answers.

## The interesting result

Most RAG projects report a retrieval score and stop. That number here is 74%, and it's the
least interesting thing in the repo.

This one is better: **the system refuses 90% of questions that have no answer, but only 40%
of the ones where retrieval quietly returned the wrong passages.** It knows when there's
nothing there. It doesn't know when there's something there that isn't what you asked for.

That second case is the dangerous one, and it's the common one in production. It works out
to **10% of answerable questions getting an answer with nothing behind it.**

You can't find that with recall or faithfulness scores. You find it by putting questions
with no answers into your test set, which most test sets don't do.

**[Read the write-up →](blog/measuring-refusal.md)**

## How it works

```
question → route → split if multi-part → retrieve (keyword + meaning) →
filter by company/form/year → rerank → answer from those passages only
```

Roughly 43,000 chunks of SEC filings, Fed statements and CFPB complaints. Hybrid retrieval,
a cross-encoder reranker, and a planner that splits comparison questions in two before
searching — but only when a cheap check says it's worth it.

Splitting is worth **+0.19 recall on multi-part questions** and nothing measurable on
single-part ones, which is why it's gated rather than always on.

[Full architecture and evaluation methodology →](docs/METHOD.md)

## What it can't do

- **It fabricates sometimes.** One question in the test set asks for a CEO's pay, which
  isn't in the corpus; the system invents a figure and cites a real filing's cover page.
  It's tracked as a known failure rather than quietly fixed.
- **Ten US banks only**, filings through 2026. Ask about anyone else and it should refuse.
- **It's slow.** About 30 seconds a question on the free demo tier, most of it reranking.
- **Not investment advice**, and not a substitute for reading the filing.

## Run it

```bash
git clone https://github.com/omvyas77/finhelm && cd finhelm
cp .env.example .env                      # add ANTHROPIC_API_KEY
export FINHELM_DATA_DIR=$PWD/data/ci      # small sample corpus, included
docker compose up -d
```

UI on `localhost:8501`, API on `localhost:8000/docs`, traces on `localhost:16686`,
experiment tracking on `localhost:5001`.

That second line matters: the real index is 961 MB and isn't in the repo. Without it the
API starts, reports itself unhealthy, and the UI waits — which is correct behaviour, and
confusing if you don't expect it. `scripts/build_index.py` builds the real one in about
85 minutes.

## Repository

| | |
|---|---|
| `src/finhelm/` | retrieval, generation, the service |
| `evals/` | golden set, metrics, the evaluation runner |
| `analytics/` | CFPB complaint outcome screening, and why it doesn't work on one metric |
| `blog/` | the write-up |
| `notes/failures.md` | every wrong answer and why, kept as it happened |
| `deploy/` | Docker Compose, Kubernetes, Cloud Run, the Space |

Every number above comes from one frozen run,
[`semantic-hybrid-rr-ctx-ag-final`](evals/results/semantic-hybrid-rr-ctx-ag-final.json).
A test asserts this README can't quote a figure that run didn't produce.

MIT licensed.
