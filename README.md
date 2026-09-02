# finhelm

**Question answering over US bank SEC filings that shows its work — and an evaluation
harness honest enough to say when it doesn't.**

Every answer is built only from passages retrieved out of real SEC filings, FOMC
statements and CFPB consumer complaints, and every passage is shown to the reader with a
link to the source document. When the retrieved passages don't contain the answer, the
system says so instead of guessing.

> **Status: Days 1–3 of 5 complete.** The service, the observability, the five-service
> container stack, the CI gate and the Kubernetes manifests all run and are verified
> against the real thing. Not yet done: the consumer-complaint analytics module, a public
> deployment, and the final full evaluation. **There is no live demo link, because nothing
> is deployed** — the Cloud Run and Space configs are written and unexecuted, and they say
> so. Sections below flag what is measured against what is merely written.

---

## The business question

A credit-risk analyst at a bank needs to answer things like *"how do Capital One and
Synchrony each describe credit normalization in their most recent 10-Ks?"*

SQL cannot answer that. The fact lives in a paragraph of Item 7 MD&A, phrased differently
by each filer, in a document nobody has parsed into columns. The existing options are
full-text search — which returns the right document and leaves you to read forty pages —
or an LLM, which will answer fluently and sometimes invent the number.

This system takes the third path: retrieve the specific passages, answer only from them,
cite each claim to a source the reader can open, and refuse when the evidence isn't there.
The refusal is the part that makes it usable in finance, and it is measured as carefully
as the answers.

---

## Results

Every number below comes from one run — **`semantic-hybrid-rr-ctx-ag-final`**, 202
questions, 248 gold spans, frozen at
[`evals/results/semantic-hybrid-rr-ctx-ag-final.json`](evals/results/semantic-hybrid-rr-ctx-ag-final.json)
and tagged. Nothing here is hand-edited or carried over from an earlier run.

| Metric | Value | What it means |
|---|---|---|
| **recall@16 (macro)** | **0.7403** [0.682, 0.793] | Share of needed evidence that reaches the model |
| recall@16 (micro) | 0.7016 [0.642, 0.755] | Same, weighted by span rather than by question |
| — single-span questions | 0.8246 (n=114) | |
| — multi-span questions | 0.5970 (n=67) | The hard tier, and the honest one |
| MRR | 0.4633 | |
| **citation validity** | **1.0000** | No answer ever cited a source that wasn't supplied |
| abstention recall | 0.9048 | Of questions with no answer in the corpus, how many it declined |
| over-refusal rate | 0.1160 | Of answerable questions, how many it wrongly declined |
| citation density | 1.0420 | Citations per substantive claim |
| route accuracy | 0.9485 | |
| cost / query | $0.0339 | |
| p50 latency | 32.3 s | Agentic path, cold cache, laptop CPU |

Both macro and micro recall are reported because they diverge once span counts are
unequal (0.740 vs 0.702), and quoting only the flattering one would be a choice. **Macro
is the headline throughout this repository**; micro appears beside it and never instead
of it.

**The retrieval half of this run is bit-identical to the run that preceded it by two
weeks** — recall@16, both span tiers and micro recall all reproduce to four decimal
places. That is worth more than any single number here: it means the ablation's
comparisons were measuring configuration rather than run-to-run drift.

Two figures did move, and both have a boring explanation rather than a story. **MRR shifted
+0.0005** while recall did not, which is what a rank permutation *inside* the top 16 looks
like — recall@16 asks whether a span is in the set, MRR asks where. **Abstention recall
went 0.8571 to 0.9048**, which is one question out of 21 negatives changing its mind
(18/21 to 19/21). At n=21 a single flip is 4.8 points, so that is sampling noise in a
generative model, not an improvement, and it is not claimed as one.

### How it got there

Each step is a paired comparison on the same golden set, not a re-tuned rerun.

| Change | recall@16 | Δ |
|---|---|---|
| Day 2 close | 0.3890 | — |
| Golden set expanded to 202 questions | 0.4171 | baseline reset |
| Contextual chunk headers | 0.4475 | +0.0304 |
| `bge-base` embeddings (from `bge-small`) | 0.4917 | +0.0373 on multi-span |
| Issuer/form metadata filtering | 0.5387 | +0.0470 |
| Sliding-window cross-encoder reranking | 0.6160 | +0.0608 |
| Context budget k=16 | **0.7403** | +0.1243 |

### Where the agentic path helps, and where it does nothing

The expensive path is only worth its cost where it is actually doing work, and the only way
to know that is to run the control. Three arms, each differing from the next in one thing,
compared by paired bootstrap over per-question recall@16 (10,000 rounds):

| | all (n=181) | multi-span (n=67) | single-span (n=114) |
|---|---|---|---|
| **decomposition + cross-query fusion** | **+0.0718** `[+0.0331, +0.1105]` | **+0.1791** `[+0.0896, +0.2687]` | — |
| per-sub-question metadata filtering | +0.0055 `[−0.0055, +0.0166]` | +0.0149 `[−0.0149, +0.0448]` | — |
| **everything agentic, on vs off** | **+0.0773** `[+0.0387, +0.1160]` | **+0.1940** `[+0.1045, +0.2836]` | +0.0088 `[+0.0000, +0.0263]` |

Bold intervals exclude zero. The rest do not.

**The shape is the result, not the size.** Multi-span questions gain a fifth of a point;
single-span questions move +0.0088 with an interval touching zero — indistinguishable from
nothing. That was the prediction registered before the run, and its falsification condition
was single-span moving too, which would have meant the delta came from something other than
splitting the question. It did not move. `route_accuracy` is 0.9485 in both arms to four
decimal places, independently confirming the flag does not disturb routing.

**That asymmetry is what justifies the router.** Decomposition costs latency and API calls
and buys nothing on the two thirds of questions that need one document. A deterministic
pre-check decides whether a question is worth splitting before any model is called, which
made decomposition fire 27 times instead of 53 — **49% faster and 49% cheaper with
bit-identical recall.**

**A caveat worth raising was worth testing, and testing retired it.** Turning the flag off
removes three things at once, and one of them — per-sub-question filtering — had
independent evidence of being valuable, with filter coverage of 75% on a compound question
against 99% on sub-questions. So the on/off delta was biased in decomposition's favour by
an unknown amount. Holding filters fixed at the sub-question level while retrieving with
the single original query put a number on it: **+0.0055, interval spanning zero.** 93% of
the effect survives.

That result is narrower and more useful than "filtering is worthless": `filters_for` on the
compound question already captures nearly all the available recall, so 24 extra points of
*coverage* do not convert into *recall*. **Coverage was the wrong proxy for value.** A
component can be measurably more thorough and still be worth zero, and no amount of
reasoning about the caveat would have produced +0.0055 — only holding it fixed did.

### What was measured and rejected

Kept here because negative results are most of the work and disappear from write-ups:

- **Pool widening** — final recall *falls* as the candidate pool grows (0.712 → 0.652 at
  width 50). Widening only helps if the scorer can use it.
- **`bge-reranker-large`** — indistinguishable from `base` once windowing is on: 9 wins,
  11 losses, 161 of 181 questions bit-identical. Verified on a T4 against locally
  computed scores that reproduced to 0.00000.
- **Per-sub-question rerank budgets** — loses on every tier. The isolation test that
  motivated it was invalid: it gave each sub-question its own top-5, measuring context
  budget rather than query shape.
- **Interleave fusion** (+0.0028) and **a different RRF constant** (+0.017, simulation
  only) — inside the noise band.

An oracle reranker at width 20 would score **0.9185**, so roughly **+0.10 of headroom sits
in the scoring stage**, not in retrieval width or in model scale.

---

## Architecture

The serving path is the top two thirds. **The evaluation loop at the bottom is the part
worth looking at** — it is what turns the rest from a demo into something with numbers
attached, and it runs on every pull request rather than when someone remembers.

```mermaid
flowchart TB
    SEC[SEC EDGAR<br/>10-K, 10-Q, 8-K] --> NORM[Normalise and section-split]
    CFPB[CFPB complaints] --> NORM
    FOMC[FOMC statements] --> NORM
    NORM --> CH[Semantic chunking<br/>800 tokens, contextual headers]
    CH --> COLF[filings<br/>24,650 chunks]
    CH --> COLC[complaints<br/>18,498 chunks]

    Q[Question] --> RT{Router<br/>keyword first, LLM if ambiguous}
    RT --> AG{Worth splitting?<br/>deterministic pre-check}
    AG -->|yes| DEC[Decompose into 1-4 sub-questions<br/>hard timeout, fails open]
    AG -->|no| SQ[Single query]
    DEC --> RET
    SQ --> RET
    COLF --> RET
    COLC --> RET
    RET[Hybrid retrieval per query<br/>BM25 + dense bge-base, RRF]
    RET --> MF[Metadata filter<br/>issuer, form, year, with backoff]
    MF --> XQ[Cross-query RRF across sub-questions]
    XQ --> RR[Cross-encoder rerank<br/>sliding window, 512-token limit]
    RR --> GEN[Generate with numbered sources]
    GEN --> ANS[Answer + citations<br/>or INSUFFICIENT_CONTEXT]

    subgraph EVAL [EVALUATION LOOP - what gates every merge]
        GOLD[Golden set<br/>202 questions, 248 spans, 21 negatives] --> RUN[run_eval.py]
        RUN --> DET[recall@16, MRR, citation validity<br/>abstention pair, route accuracy]
        DET --> STAT[Wilson intervals, paired bootstrap<br/>split by span count]
        STAT --> ML[MLflow]
        STAT --> GATE{CI gate}
        GATE -->|every push| T1[tests, 3 min, free]
        GATE -->|pull requests| T2[deterministic eval<br/>committed fixture, free]
        GATE -->|pull requests| T3[DeepEval + regression<br/>vs accepted baseline]
    end

    ANS --> RUN
```

**Corpus:** 10 institutions — AXP, BAC, C, COF, DFS, GS, JPM, SYF, USB, WFC — as 24,650
filing chunks plus 18,498 CFPB complaint chunks.

**Serving:** FastAPI, a Streamlit demo, OpenTelemetry spans exported to Jaeger, and MLflow
tracking. Five compose services, three of them the same image so the MLflow server can
never be a different version than the client that wrote its store.

---

## Evaluation methodology

The part of this project that took the longest and is worth the most.

**Ground truth is anchored on `(doc_id, snippet)`, not on chunk ids.** A chunk id is not
portable across chunking strategies, so a golden set keyed on one cannot compare them. A
retrieved chunk counts as a hit when the document matches exactly, it shares a contiguous
10-word run with the gold snippet, and its figures do not contradict the gold span's.

**Retrievability ceiling is 1.0000** for all three chunking strategies — every gold span
is reachable by *some* chunk. Low recall is therefore genuine retrieval failure, not a
metric artifact. That check is what makes the recall number mean anything.

**Golden set: 202 questions, 248 gold spans.** Composition: 114 single-hop, 42 multi-hop,
25 temporal, 13 unanswerable-but-in-domain, 8 out-of-scope. Provenance, stated exactly:
21 hand-written (all the negatives), 127 LLM-drafted and machine-verified, and **54
LLM-drafted and still pending human review**. The negatives are hand-written because
models are bad at inventing plausible-but-absent facts, and those are the highest-value
items in the file.

**Negatives are scored separately.** Faithfulness against an empty ground truth is
meaningless, so the 21 negatives are scored by the abstention pair instead. Reporting both
directions matters: a system that refuses everything scores perfectly on one and
catastrophically on the other.

**The judge is a different model family from the generator** (Gemini judging Claude),
enforced in `Config.__post_init__`, because same-family judging produces self-preference
bias.

**Comparisons use paired bootstrap over questions, not point estimates.** This is the
single most important thing learned here: with 202 questions the paired sd is such that
the resolvable effect is about 0.12, and most interventions land near +0.02. An
18-cell ranking built from point estimates was noise with an ordering printed on it.

### The CI gate

Three tiers, and the shape was decided by a measurement that refuted the plan:

| | runs on | cost | what it gates |
|---|---|---|---|
| `fast` | every push | $0 | unit tests, including the tests covering the gate itself |
| `eval` | pull requests | $0 | a real retrieval eval on a committed fixture |
| `judged` | pull requests | cents | DeepEval smoke suite, regression vs baseline |

The intent was two tiers, with the retrieval eval on every push — a gate that only
re-reads recorded numbers gates nothing about the code in the diff. It does run a real
eval, against a committed 1,903-chunk fixture carved out of the real corpus
(`scripts/make_ci_fixture.py` selects gold-bearing chunks using the metric's own `is_hit`,
so the fixture cannot disagree with the metric that reads it).

It just cannot run on every push. On a 2-vCPU runner the first attempt was **cancelled at
question 27 of 40 by a 25-minute timeout**, at 49 seconds per question against ~5 seconds
locally — a 33-minute projection. So the eval moved to pull requests, and the every-push
tier is what it can honestly be. What is lost: a push straight to a branch with no PR gets
no retrieval check. What is kept: the check, when it runs, is real.

Part of that cost was self-inflicted and is worth the warning. `OMP_NUM_THREADS=1` is
required on macOS, where faiss-cpu and torch each load an OpenMP runtime into one process
and dense retrieval segfaults. Linux wheels carry no such conflict, so importing that
workaround into CI bought nothing and pinned the cross-encoder to one of two vCPUs.

**The floor of 0.80 against a measured 0.8167 is a tripwire, not a quality number** —
retrieval against 1,903 chunks is far easier than against 24,650.

The gate is built to be unable to pass quietly:

- an **unknown metric name is a failure**, not a pass. The obvious threshold to write here
  is `recall_at_5`, which this system does not produce — it serves top-k=16 — so a lenient
  lookup would sit green forever while reading exactly like a gate;
- a metric present but `None` fails, because "not measured" is not "passed";
- `--fail-on-fallback` catches a missing index being silently substituted, which would
  mean the gate measured a different system;
- `--deterministic-only` asserts `llm.USAGE` is empty afterwards rather than trusting the
  flags meant to arrange it — decomposition calls a model and catches every exception, so
  a keyless run would not error, it would quietly stop splitting multi-hop questions.

**Proof the gate is real rather than decorative.** The same command, with the context
budget cut from 16 to 2 and nothing else changed:

| config | recall@16 | multi-span | single-span | gate |
|---|---|---|---|---|
| `top_k_context=16` (shipped) | 0.8167 | 0.8438 | 0.7857 | **passes**, exit 0 |
| `top_k_context=2` (broken) | 0.5333 | **0.4375** | 0.6429 | **fails**, exit 1 |

```
GATE FAILED
  x recall_at_16 = 0.5333 is below the floor of 0.8000
```

Multi-span is hit about three times as hard as single-span (-0.406 against -0.143), which
is the shape this break should produce: fewer context slots cost most where a question
needs evidence from several documents.

An earlier version of this table showed single-span recall as *identical* across the two
arms, and that was a property of the fixture rather than of the change. The fixture was
rebuilt when the judged tier turned out to be missing evidence for 7 of its 12 questions,
and on the rebuilt corpus the gold chunk for a single-span question is not always inside
the top 2, so cutting the budget costs that tier too. The claim was true of an artifact,
and when the artifact changed it needed re-measuring rather than repeating.

The damage lands where it should: single-span recall is untouched and multi-span nearly
halves, because a smaller context budget can only hurt questions that need more than one
passage. A gate that fired without that pattern would be measuring something else.

---

## The analytics module, and the measurement that says it does not work

[`analytics/complaint_disparity.py`](analytics/complaint_disparity.py) screens CFPB
complaint outcomes for disparity: relief rate and timely-response rate per
(company, product) cell, Wilson intervals, two-proportion z-tests against the same product
at every *other* company, Benjamini-Hochberg across the whole family of tests.

**It is a screening methodology, not a finding about any company.** A flagged cell warrants
investigation; it does not establish that anyone was treated unfairly. Companies are named
because the data names them, and no conclusory claim about any of them appears anywhere in
this repository.

The interesting output is a negative result about the screen itself:

| outcome | cells tested | flagged after BH | median \|difference\| flagged / quiet |
|---|---|---|---|
| relief rate | 80 | **52 (65%)** | 0.158 / 0.047 |
| timely response | 80 | **6 (8%)** | 0.027 / 0.009 |

A screen that flags two thirds of what it tests is not detecting anomalies. The obvious
suspicion is a power artifact — enough complaints and trivial gaps reach significance — and
that is not what is happening: flagged cells differ by a median of **sixteen percentage
points**, at similar cell sizes to the quiet ones. The effects are large and real.

**The comparison group is wrong.** CFPB's product taxonomy has four values, and
"Debt collection" contains national banks, debt buyers and credit bureaus — businesses
whose *role* in a complaint differs so fundamentally that a shared relief rate is not a
meaningful expectation. The same screen on timely response flags 8%, because timeliness is
a procedural obligation that means the same thing for every firm regardless of business
model. Same statistics, same cells; one usable screen and one unusable one, and the
difference is entirely whether the peer group is real.

The module prints that diagnostic on every run and warns when the flag share exceeds 25%.

[`analytics/METHODOLOGY.md`](analytics/METHODOLOGY.md) covers ecological inference,
selection bias, confounding, the SR 11-7 and ECOA/Reg B context, and the six things a
fair-lending reviewer would demand next — none of which public complaint data supports.
The ACS/ZCTA geographic join the build guide suggests is **deliberately not built**: it
would be the most misreadable output in the repository, and doing it responsibly requires
the caveats to travel with every number, which a CSV does not do.

---

## Quickstart

```bash
git clone https://github.com/omvyas77/finhelm && cd finhelm
cp .env.example .env    # add ANTHROPIC_API_KEY and GOOGLE_API_KEY
docker compose up -d
```

Then: UI at `localhost:8501`, API at `localhost:8000/docs`, traces at `localhost:16686`,
MLflow at `localhost:5001`.

The indexes are build artifacts and are not in the repo (961 MB). Build them with
`scripts/build_index.py`, or point `FINHELM_DATA_DIR` at the committed CI fixture in
`data/ci` to run against the small corpus immediately.

**On an 8 GB machine, give Docker at least 5 GB.** A single `/ask` peaks at 2.7 GiB: both
models resident plus the FAISS index and its metadata.

---

## Scaling

The `VectorStore` protocol means the backend is a config change, not a rewrite. A
benchmark against Postgres + pgvector on identical vectors found latency a wash at this
size (p50 1.6 ms FAISS vs 1.7 ms pgvector) — the real difference is filtering. FAISS has
no `WHERE` clause, so it over-fetches and post-filters; at 0.1% selectivity it returns
**0 results where 26 matching rows exist**, silently. Postgres applies the predicate in
the query.

**Kubernetes manifests are in [`deploy/k8s/`](deploy/k8s/)** — Deployment, Service, HPA,
ConfigMap, Secret, PVC and PDB, validated against a local `kind` cluster and torn down.
They are not what runs the demo. For single-user traffic a cluster is unjustified cost and
operational overhead; they exist so the scaling path is concrete rather than hypothetical.

What that validation actually established, and one thing it did not: all seven manifests
were accepted by a real API server, the Deployment reached `Available 1/1`, the PVC bound,
`securityContext` applied as `uid=10001` non-root, and the Service's EndpointSlice
selected the pod. The container itself was **not** exercised there — the image is 5.76 GB
and was never pushed to a registry, so the structural check ran on `busybox`. The compose
stack is what verifies the real container end to end.

The finding worth keeping is a silent one. `ReadOnlyMany` is correct for production, since
every replica reads the same immutable index; kind's default StorageClass cannot serve it,
and the failure gives you nothing to go on — the PVC stays `Pending` reporting only
"waiting for first consumer", pods stay `Pending`, and **no event on either object ever
mentions the access mode.** Changing that one field to `ReadWriteOnce` bound it in six
seconds.

**Cloud Run and Hugging Face Space configs** are in [`deploy/cloudrun/`](deploy/cloudrun/)
and [`deploy/hf-space/`](deploy/hf-space/) — and **neither is deployed.** There is no GCP
project or Space yet, so no live URL exists and the cold-start figures in those READMEs
are derived from local measurement rather than observed. The named risk is that a 5.76 GB
image is large for scale-to-zero; if cold pulls prove too slow, the honest fixes are a
serving-only image without the eval harness, or paying for `minScale: 1`.

Still to build: incremental re-indexing as new filings land, and a cache keyed on
question + config.

---

## Limitations

Stated plainly, because they are the first thing a careful reader will look for.

- **The corpus is 10 institutions.** Anything outside them is out of scope by
  construction, and the system is expected to refuse.
- **54 of 202 golden questions are LLM-drafted and not yet human-reviewed.** The other
  148 are hand-written or machine-verified. The headline number includes all 202.
- **About half the multi-span questions yoke arbitrary facts** — two gold snippets sharing
  under 10% of their content. That is a property of how the set was generated, and it
  partly explains the 0.597 multi-span recall.
- **One embedding model was evaluated end to end.** `bge-large` was never indexed.
- **Complaint analysis is ecological, and its peer group is wrong.** CFPB's finest
  geography is a 3-digit ZIP prefix, so any demographic association is area-level and
  cannot support individual-level conclusions. Separately, the disparity screen flags 65%
  of cells on relief rate because the four-value product taxonomy mixes banks, debt buyers
  and credit bureaus into one baseline — measured, not suspected, and documented in
  [`analytics/METHODOLOGY.md`](analytics/METHODOLOGY.md).
- **The consumer-dispute rate cannot be computed.** CFPB stopped publishing
  `consumer_disputed` in April 2017 and it is absent from this extract.
- **Section parsing falls back on a minority of filings**, so some chunks carry a
  `full_document` section label rather than a real item.
- **q055 is a deliberate failing test.** The system fabricates an executive's compensation
  and cites a real 8-K cover page, which scores 1.0 on citation validity — a case where
  the metric is satisfied and the answer is wrong. It is marked `xfail(strict=True)` so
  that fixing it turns the gate loudly XPASS rather than quietly green.

A running log of every failure and its mechanism is in
[`notes/failures.md`](notes/failures.md).

---

## License

MIT — see [LICENSE](LICENSE).
