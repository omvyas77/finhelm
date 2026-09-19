# finhelm

[![eval-gate](https://github.com/omvyas77/finhelm/actions/workflows/eval-gate.yml/badge.svg?branch=main)](https://github.com/omvyas77/finhelm/actions/workflows/eval-gate.yml)
[![live demo](https://img.shields.io/badge/demo-Hugging%20Face%20Space-blue)](https://huggingface.co/spaces/omvyas77/finhelm)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Ask questions about US bank SEC filings. Get answers built from the passages they cite,
or a refusal when the filings don't say.**

**[Try it →](https://huggingface.co/spaces/omvyas77/finhelm)**  The first question after
idle is slow while the demo wakes up.

| Finds the evidence | Refuses when it should | Refuses when it shouldn't | Cost per question |
|:--:|:--:|:--:|:--:|
| **74%** | **95%** (18 of 19) | **12%** (22 of 183) | **$0.03** |

Measured on 202 questions. 181 were drafted by Claude Opus from sampled filing passages and
checked by script: each gold passage is verbatim from its filing and findable in the index.
The other 21 were written by hand to have no answer in the corpus, and two of those turned
out to have one.

---

## Why this exists

A credit analyst wants to know how Capital One and Synchrony each describe credit
normalization in their latest 10-Ks. SQL can't answer that. The fact is a paragraph of
Item 7, worded differently by each filer. Full-text search returns the right document and
leaves you forty pages to read. A chatbot answers fluently and sometimes invents the
number.

This retrieves the specific passages, answers only from them, shows you each one with a
link to the filing, and says "I don't have that" when the evidence isn't there.

The refusal is the part that makes it usable in finance, so it's measured as carefully as
the answers.

## What the evaluation found

The failure this project used to lead with turned out to be a mistake in the test set.
Question q055 asks for Jamie Dimon's 2025 compensation. I wrote it as unanswerable,
assuming CEO pay is only disclosed in the proxy statement, which isn't in the corpus. But
JPMorgan also files the figure in an 8-K each January, that 8-K is in the corpus, and the
system's answer of $43 million quoted it exactly. The model was right and the label was
wrong. Checking the other 20 hand-written negatives against the full corpus turned up one
more (q063). Both are relabelled and the run re-scored; the model's outputs are unchanged.

What holds up after that is narrower. The system refuses 18 of the 19 questions that have
no answer. On 31 answerable questions, none of the passages that answer them reached the
model. It declined 13 of those and answered 18. I read all 18:

| What the answer was | Questions |
|---|:--:|
| right, from a passage the scorer doesn't credit | 8 |
| a refusal, stated partway through the answer | 2 |
| grounded in real passages, but answering a nearby question | 7 |
| wrong | 1 |

None of them invented a figure. The wrong one (q188) asked how Capital One's liquidity
disclosure changed between its 2024 and 2026 10-Ks, and got back the 2025 10-K's numbers
labelled as the 2024 filing's. Every figure is real and cited to the passage it came from.
The year is wrong. A citation check passes it.

So the system is good at recognising that nothing relevant is there, and weaker when
something close is there: a different year's filing, a neighbouring risk factor, the Fed's
comments about the ECB instead of the ECB. Recall and faithfulness scores don't show this.
You find it by putting unanswerable questions in the test set and then reading what the
system did when retrieval missed.

**[Read the write-up →](blog/measuring-refusal.md)**

## How it works

```
question → route → split if multi-part → retrieve (keyword + meaning) →
filter by company/form/year → rerank → answer from those passages only
```

Roughly 43,000 chunks of SEC filings, Fed statements and CFPB complaints. Hybrid retrieval,
a cross-encoder reranker, and a planner that splits comparison questions in two before
searching, but only when a cheap check says it's worth it.

Turning the planner off costs **0.19 recall on multi-part questions** (95% interval 0.10 to
0.28) and nothing measurable on single-part ones, which is why it's gated rather than
always on. That comes from a paired comparison against an agentic-off control run, written
up in [`notes/failures.md`](notes/failures.md#what-decomposition-buys-measured-against-a-real-control).

[Full architecture and evaluation methodology →](docs/METHOD.md)

## What it can't do

- **It sometimes answers from the wrong document.** Asked to compare two years of Capital
  One's 10-Ks, it used a third year's figures for one of them. Asked about the ECB, which
  isn't in the corpus, it answered from the Fed's minutes and set aside the newest of them
  as "future-dated".
- **Not every sentence is cited.** 28% of the sentences in its answers carry no source
  marker, including some that state figures. Check numbers against the passages shown.
- **Ten US banks only**, filings through 2026. Ask about anyone else and it should refuse.
- **It's slow.** The median question took 32 seconds in the evaluation run, most of it
  spent finding and ranking passages before the answer is written.
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
API starts, reports itself unhealthy, and the UI waits, which is correct behaviour and
confusing if you don't expect it. `scripts/build_index.py` builds the real one, in about
90 minutes on CPU for the filings index.

## Repository

| | |
|---|---|
| `src/finhelm/` | retrieval, generation, the service |
| `evals/` | golden set, metrics, the evaluation runner |
| `analytics/` | CFPB complaint outcome screening, and why it doesn't work on one metric |
| `blog/` | the write-up |
| `notes/failures.md` | every wrong answer and why, kept as it happened |
| `deploy/` | Docker Compose, the Hugging Face Space, Kubernetes manifests (tested on a local kind cluster) |

The headline numbers come from one frozen run,
[`semantic-hybrid-rr-ctx-ag-final`](evals/results/semantic-hybrid-rr-ctx-ag-final.json),
re-scored after the two label fixes. `tests/test_readme_traces_to_a_run.py` recomputes each
figure on this page from that run and fails if any has drifted. The 0.19 comes from the
comparison linked above.

MIT licensed.
