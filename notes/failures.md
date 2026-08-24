# Failure log

Every wrong answer, with the *mechanism* — not just "it was wrong."
This file becomes the golden set (Day 2), the failure taxonomy (2.9), and the blog post (Day 5).

Format:

| # | Question | What happened | Mechanism | Config |
|---|---|---|---|---|

Mechanism vocabulary (keep it consistent so the taxonomy counts are meaningful):
`retrieval_miss` · `right_chunk_wrong_synthesis` · `temporal_confusion` ·
`over_refusal` · `under_refusal` (answered when it should have abstained) ·
`routing_error` · `fabricated_citation` · `uncited_claim`

---

## Ingestion findings (Day 1.1)

**DFS is not in SEC's `company_tickers.json`.** Capital One completed its acquisition of
Discover in May 2025, so the ticker is delisted. Historical filings remain under CIK
0001393612 and are reachable by CIK override. Only 16 filings vs. ~50 for live tickers,
and the most recent 10-K is FY2024 (filed 2025-02-20). Keeps the COF/DFS merger as a
genuinely interesting multi-hop question.

**10-K section extraction: 18/30 (60%).** Failures are perfectly per-company — all three
years fail together — so this is heading convention, not parser flakiness. Three causes:

All 12 failures turn out to share **one root cause**: these four companies present the
10-K as an annual-report narrative fronted by a "Form 10-K Cross-Reference Index." The
item headings appear exactly once, in that index, mapped to page ranges. The body uses
descriptive chapter titles (`Managing Global Risk`, `Risk Factors Relating to Our
Business`) with no item anchors to regex against. There is nothing to match.

My first diagnosis said C and SYF were regex-fixable — that was wrong, and checking the
body rather than the TOC is what disproved it.

WFC and USB had a *second*, worse problem on top: their 10-K primary document is a thin
wrapper whose items read "information in response to this item can be found in the annual
report," with the substance in **Exhibit 13**. That is a corpus-content bug, not a
metadata one, and it was fixed by following the filing index to the EX-13 document:

| | before | after |
|---|---|---|
| WFC FY2025 | 74K chars | 941K chars |
| USB FY2025 | 247K chars | 732K chars |

## Ingestion findings (Day 1.4)

Two dedupe bugs, both silent, both found by checking *which* records disappeared rather
than trusting the drop count.

**`doc_id` was not unique.** `TICKER_FORM_DATE` collides because companies file several
8-Ks on the same day — JPM did it repeatedly. 29 legitimate distinct filings were being
discarded as "duplicates." Fixed by appending the accession sequence:
`JPM_8K_2026-07-23_000123`.

**The 500-char prefix hash was destroying year-over-year sections.** This is the one that
would have quietly ruined Day 2. SEC sections open with identical stock language every
year — *"The following discussion sets forth the material risk factors that could affect
JPMorganChase's financial condition..."* — so hashing the first 500 characters made
FY2024, FY2025 and FY2026 Risk Factors look like the same document. Three years collapsed
to one for JPM, BAC, and GS.

The golden set allocates **8 temporal questions** ("how did X's credit language change
from 2023 to 2025?"). Every one of them would have been unanswerable, and the failure
would have looked like a retrieval problem rather than an ingestion problem — days of
debugging in the wrong place.

Fixed by hashing full normalized text instead. Only *exact* duplicates are safe to drop
automatically. Near-duplicates are now counted and reported (112 records share a 500-char
opening) but deliberately retained.

| | before | after |
|---|---|---|
| EDGAR records kept | 355 | 403 |
| 10-K section records | 34 | 48 |
| 8-K records | 261 | 295 |
| Risk Factors years per company | 1 | 3 |

Remaining 734 drops are all CFPB — exact-duplicate complaint narratives, genuinely
redundant.

**Corpus: 17,611 records, 77.6 MB** (403 EDGAR · 75 FOMC · 17,133 CFPB).

---

## Ingestion findings (Day 1.3)

**`format=json` returns 404.** It appears in the build guide's snippet. The endpoint
already returns JSON; passing the parameter routes to a nonexistent export path. Found by
bisecting parameters one at a time against a known-good bare request.

**Offset pagination is silently ignored.** `frm`, `from`, and `offset` all return the
*identical* window — no error, no warning. Naive paging would have produced 5,000 copies
of the same 1,000 records. Deep paging requires the `search_after` cursor, formatted
`{epoch_ms}_{complaint_id}` from the previous page's `sort` key. `size=5000` also works
in a single request.

**The sampling bug that mattered most.** The API sorts newest-first, so taking the first
5,000 per product collapsed the sample to **2026-02-25 → 2026-07-29** — five months —
even though `date_received_min=2023-01-01` was set. The filter was doing nothing. Every
temporal question in the golden set, and the whole multi-year premise of the Day 4
disparity module, would have been built on five months of data.

Fixed by stratifying: one request per product × calendar quarter, ~358 each, 2023Q1
through 2026Q2. Result is ~1,430 complaints per quarter, evenly spread.

Residual bias, documented not fixed: within each quarter the sample is still newest-first,
so records cluster toward quarter-end. Acceptable for retrieval; the Day 4 methodology
note should mention it.

**`consumer_disputed` does not exist for this date range** (0/200 records). CFPB
discontinued the field in 2017. The build guide lists consumer-dispute rate as one of
three Day 4 metrics — that metric is not computable and the module should ship with
relief rate and timely-response rate only.

**26% of ZIP codes are masked** (`010XX` style). Enough full ZIPs remain for the
ZCTA-level demographic join, but the effective sample for the geographic layer is ~74%.

---

## Ingestion findings (Day 1.2)

**Silent encoding corruption in FOMC text.** federalreserve.gov serves UTF-8 but omits
`charset` from the Content-Type header, so `requests` falls back to ISO-8859-1 per the
HTTP spec. En dashes arrived as mojibake — `"approved by a 9 â 3 vote"` instead of
`9 – 3`. Nothing errors; the corpus just quietly degrades, and the cache persisted the
bad decode so a re-run wouldn't have healed it.

Caught by eyeballing one extracted statement rather than trusting the document count.
Worth repeating for every new source: check the *text*, not just the row count.

---

Section extraction stayed at 60% after the fix (the EX-13 annual reports are narrative
too), but the retrievable text for those two companies grew ~10× and ~3×. Worth
separating in the README: **content coverage** and **section labelling** are different
problems and only one of them got solved.

**Bug found and fixed:** `re.search` returns the first match, which is always the
table-of-contents entry. The 5,000-char length guard rejected it and the function gave up
instead of scanning later matches. Switched to `re.finditer` + longest capture group.
This alone took extraction from 0% to 60%.

---

## HTML extraction findings (Day 1.5/1.7)

Found while spot-checking BM25 output, not while writing the extractor. The lesson
repeats: **row counts were correct the whole time; the text was wrong.**

**1. Block elements were concatenated without a delimiter.** `selectolax`'s `.text()`
defaults to `separator=""`, so `UNITED STATES</div><div>SECURITIES` extracted as
`STATESSECURITIES`. This hit **402 of 403 EDGAR docs** (48,901 glued tokens, 0.89% of all
words) and every FOMC document. These tokens are unmatchable by BM25 and unrecoverable at
query time — no tokenizer can split them back apart. Fixed with `separator=" "`.

**2. Inline-XBRL hidden fact blocks survived extraction.** Filings carry a hidden block of
tagged facts (CIK, axis members, repeated dates, company name). It renders as nothing but
extracted as a short, keyword-dense chunk — and BM25's length normalisation ranked those
*above* real prose. `"JPM CET1 ratio"` returned three cover-page metadata dumps as its top
three hits.

Filers hide it two ways and only one is reachable from CSS: **selectolax cannot select
namespaced tags** — `css(r'ix\:hidden')` silently matches zero nodes rather than raising.
The `<ix:hidden>` form had to be stripped from the raw markup before parsing. Junk chunks:
77 → 19 (style selector) → 6 (regex strip). The final 6 are genuine COF securities tables
that slipped the table-drop heuristic, not boilerplate.

**3. Fixing (1) broke every money figure.** Filers put the currency symbol, the digits and
the closing paren of a negative in separate inline elements, so the separator that
un-glued words also split `$1.2` into `$ 1.2` and `(1,234)` into `( 1,234 )`. 64,704 of
84,579 dollar figures were affected — BM25 would have lost the exact amounts it is in the
pipeline to match.

Nearly missed this. A naive `\$[0-9]` count showed a **76% drop** and looked like
catastrophic data loss; the total `$` count was identical (97,726) in both versions, which
is what proved it was a spacing artifact rather than deletion. Fixed with a `_tighten`
pass. Verified by exact restoration: `$N` back to 84,579, `$ N` and `( N` to zero.

**Net effect:** +348K real words, glued tokens 48,901 → 6,760, percentages recovered
16,537 → 28,052.

**Hypothesis I got wrong:** I predicted the separator bug was also what capped 10-K
section extraction at 60%, since the item headings sit at block boundaries. Tested it —
5/12 docs before, 5/12 after. No effect. The 60% remains the cross-reference-index problem
documented above, and the two are unrelated.

**Router bug, same session.** The keyword router used substring matching, so `"cre"` in
`"credit card"` routed a pure complaints question to filings. The three-letter finance
abbreviations (`cre`, `sec`, `eps`) are all substrings of common words. Switched to
whole-token regex matching with an optional plural — whole-token alone was too strict,
since real questions say "late fees", not "late fee".

---

## Day 1.9 spot-check — 15 questions

Ran 15 questions across all three sources (4 filings single-hop, 2 temporal, 2 FOMC,
3 complaints, 1 cross-source, 3 designed to be unanswerable).

**Citation validity: 15/15 clean.** Zero invented source numbers — no `[S9]` when only 8
sources existed, across every question. The numbered-source prompt is doing its job.

**Abstention: 3/3 correct.** Tesla deliveries, Bitcoin price, and 2028 forward-looking NII
all returned the `INSUFFICIENT_CONTEXT:` sentinel rather than answering from general
knowledge. Over-refusal on answerable questions: 0/12.

**Temporal handling was better than expected.** "How did JPM's CET1 ratio change between
2024 and 2025?" retrieved only 2025 10-Qs — which looked like the classic wrong-fiscal-year
failure until I read the answer. The 2025 10-Qs carry December 31, 2024 comparatives, so it
correctly reconstructed the full series (15.7% → 14.8%) and attributed the decline to RWA
growth. Retrieving the "wrong" year was right; judging retrieval by document date alone
would have scored this as a failure.

### FAILURE — router sent a two-sided question to one collection

*"Do banks discuss credit card late fees differently than consumers complain about them?"*

Routed to **complaints only**. The complaints vocabulary matched twice ("complain", "late
fees"); the filings vocabulary matched zero times, because the question names its second
side with generic words ("banks", "discuss") that are deliberately not in the term list.

Two distinct defects, and the second is the dangerous one:

1. **Router.** Keyword counting cannot see comparative intent.
2. **Generation.** Given only complaint narratives, the model still produced a section
   titled "How Banks Frame Late Fees" and asserted a "clear disconnect" — describing one
   side of a comparison from evidence about the other side. It did not abstain, did not
   flag the missing half, and cited 7/8 sources, so **every mechanical signal said the
   answer was well-grounded.** Citation validity and abstention rate both looked perfect
   on a materially wrong answer.

This is the strongest argument in the project for judged faithfulness metrics rather than
citation-counting alone, and it belongs in the Day 2 golden set as a multi-hop case.

**Fixed (router half):** comparative phrasing ("differently", "versus", "compared to",
"consistent with", ...) now overrides a one-sided keyword verdict and escalates to the LLM
router. Verified on 6 routing cases including two that must *not* escalate. After the fix
the same question returns a genuinely two-sided answer citing SYF's $2.7B/$2.5B late-fee
income and the CFPB safe-harbor rule alongside the consumer narratives, 8/8 sources cited.

The generation half is **not** fixed — the prompt does not require the model to check that
it has evidence for every side of a comparison. Candidate Day 2 prompt change.

### Performance bug — store reloaded on every query

`load_store` was uncached while the BM25 index was cached, so every query re-read and
re-parsed `meta.jsonl` (62 MB for complaints, 50 MB for filings). A two-collection query
spent ~70s on disk before embedding anything. Cached: 68.5s cold → 168ms warm.

Worth noting the FAISS design tradeoff this exposes: the flat index keeps a full copy of
the corpus text in memory to serve metadata, which is exactly the cost pgvector avoids on
Day 3.

### Known measurement weakness — `uncited_sentences` over-counts

The metric flags any 5+ word sentence with no `[S#]` marker, which catches markdown
headings ("Key Disconnect") and summary transitions. Q12 scored 10 uncited sentences when
the real number of uncited factual claims was near zero. It is a diagnostic today; it needs
to exclude headings and non-assertive sentences before Day 4 treats it as a gate.

---

## Day 2

### Hallucinated executive compensation with a valid citation (q055)

The single most useful result of Day 2, produced by the PR gate on its first real run.

Asked *"What was Jamie Dimon's total compensation for fiscal year 2025?"* — an
`unanswerable` negative, because compensation lives in the DEF 14A proxy and the corpus
holds 10-K/10-Q/8-K only — the system did not abstain. It answered:

> Jamie Dimon's total compensation for fiscal year 2025 was **$43,000,000**, compared to
> $39,000,000 the prior year [S2]. Base salary: $1,500,000 [S2]. Performance-based
> variable incentive: $41,500,000, of which $5,000,000 cash and $36,500,000 in PSUs [S2].

`[S2]` is the cover page of a JPM 8-K — the "emerging growth company" checkbox
boilerplate. It contains no compensation figures at all. Every line of that itemised
breakdown came from the model's parametric knowledge of Dimon's actual pay, not from the
retrieved text, in direct violation of prompt rule 3 ("Do not guess, and do not answer
from general knowledge").

**Why this matters more than a typical hallucination:** it scores
`citation_validity = 1.0`. The metric checks that `[S2]` refers to a source that was
supplied, which it does. Nothing in the deterministic metric suite catches this. A
dashboard built on citation validity alone would show a perfect score on a fabricated
executive compensation figure — the highest-stakes category of number in the corpus.

This is the concrete argument for judged faithfulness alongside citation counting, and it
is why the negatives in the golden set assert abstention directly rather than being
scored by an LLM: whether the system emitted `INSUFFICIENT_CONTEXT` is a fact, not an
opinion.

Tracked as a strict xfail in `tests/test_smoke_deepeval.py`. Candidate fixes for Day 3,
in order of preference: require the model to quote the span supporting any figure before
emitting it; add a document-type precondition (compensation questions require a DEF 14A
in the context); raise `top_k_retrieve` so the abstention decision sees more evidence.

### Quota exhaustion masquerading as timeouts, then as NaN

Ragas judge calls against `gemini-3.5-flash` failed as `TimeoutError`, which surfaced as
`NaN` metrics, which numpy then propagated into a `NaN` mean for the whole run. The
underlying cause was `RESOURCE_EXHAUSTED`: tenacity retried the rate limit silently until
the per-job timeout fired. Nothing in the output ever said "rate limited", and the
headline number simply went blank.

Three separate fixes, each addressing a different layer:
  - judge switched to `gemini-3.1-flash-lite`, which has throughput for a full pass;
  - `max_workers` 16 → 4, since the default concurrency was self-inflicting the 429s;
  - aggregation now means over non-null rows and reports `n_failed` per metric, so a
    partial failure degrades the number visibly instead of erasing it.

The same bug in a different costume broke the DeepEval gate: every test passed in
isolation and half failed when run together, because DeepEval fans a metric out into one
judge call per extracted claim. Fixed with `async_mode=False`.

### `chunk_id` is not portable across chunking strategies

Ground truth was originally anchored on `chunk_id`. The same id maps to 4,566 characters
under `fixed` and to a single "." under `semantic`. Every ablation comparison would have
been silently mis-scored while producing entirely plausible-looking numbers. Ground truth
is now `(doc_id, snippet)` with contiguous 10-gram matching plus a numeric-contradiction
guard; `doc_id` is verified set-equal across all three strategies (460 docs).

Bag-of-words overlap was tried first and produced 12/18 false positives on formulaic MD&A
prose — and biased toward `sentence_window`, which has ~15x more chunks and therefore
more chances to clear a bag-of-words threshold. That artifact would have won the chunking
ablation on its own.

### Corpus coverage does not match the ablation grid

Only `filings` was chunked under all three strategies; `complaints` exists as `fixed`
only, and `filings_sentence_window` has chunks but no FAISS index. Non-`fixed` runs
crashed in `faiss.read_index`.

Rather than erroring or silently substituting, `_resolve_strategy` falls back and
*records* the substitution, which the runner writes into `history.jsonl` and the ablation
table renders as a footnote. A row captioned "semantic" that quietly ran half its corpus
as `fixed` would overstate what was compared.

The fallback is also retriever-aware, which recovered a real cell: BM25 builds from the
chunk parquet at load time and needs no FAISS index, so `sentence_window + bm25` is
measurable today while its dense counterpart waits on a 3.5-hour index build.

### A judge threshold that no retriever could ever have met

The DeepEval gate failed four positives on `ContextualRelevancyMetric` at a threshold of
0.30, with scores clustered at 0.125, 0.12 and 0.16. The cluster is the tell: real quality
variation does not land three questions within a hair of the same number.

That metric scores the *fraction* of supplied context that bears on the question. We supply
`top_k_context=8` chunks and a typical question is answered by exactly one of them, so the
achievable score is bounded near 1/8 = 0.125 no matter how good retrieval gets. The
observed scores were at or above that ceiling — retrieval was doing as well as the metric
permits, while the threshold called it a failure.

The general trap: contextual relevancy is **diluted by `top_k_context`** and therefore not
portable across context sizes. A 0.70 threshold copied from a quickstart encodes that
quickstart's `k`. Comparing this number against a run with a different `k` is comparing two
different metrics.

Set to 0.10, documented as k-dependent, and scoped to catching "almost nothing relevant
came back" rather than grading precision. Gate went green: 8 passed, 4 skipped, 1 xfailed.

### End-to-end latency silently replacing retrieval latency in the ablation table

`latency_ms` was defined as `retrieval_ms + generation_ms`. Every sweep cell was run
`--retrieve-only`, so its p50 was pure retrieval — but the final generation run shares a
config key with its retrieve-only twin, and the table's "widest run wins" tie-break would
have let it take the winning cell carrying several seconds of Anthropic round-trip.

Caught before it landed by noticing an existing generation run in `history.jsonl`:
`semantic-hybrid-ragassmoke` reports p50 **7963ms** against **1751ms** for the identical
retrieval config retrieve-only. The best row would have rendered as 4.5x slower than its
neighbours — an argument against the winning config that the data does not support, with
no error anywhere.

Fixed by reporting `p50/p95_retrieval_ms` separately and pointing the table at those. Two
notes on the fix:

  - the runner now records `retrieve_only` explicitly rather than letting the table infer
    it from a near-zero `cost_usd_per_query`. Retrieve-only runs are *not* free — the
    router still makes an LLM call — so "cost is zero" was never the right test, and a
    cost threshold would need re-tuning whenever the router or pricing changed;
  - the legacy fallback to the combined column is gated on `retrieve_only`, because for
    those runs `generation_ms` is 0 and the two figures are equal by construction. All 14
    previously measured cells render byte-identical after the change, which is what makes
    the fallback safe to keep rather than a source of quiet drift.

Same shape as every other serious bug this week: a plausible number, no exception, and a
wrong conclusion waiting for whoever read the table.

### The free tier has two quotas and I had only defended against one

`src/finhelm/judge.py` paces requests to stay under **15 requests/minute**, which fixed the
DeepEval gate. The Ragas pass on the final run then died at job 74 of 128, every remaining
job surfacing as `TimeoutError()` — the same symptom as before, so the same fix looked like
it should apply. It did not.

A single probe call gave the real answer:

```
Quota exceeded for metric: generativelanguage.googleapis.com/
generate_content_free_tier_requests, limit: 500, model: gemini-3.1-flash-lite
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
```

**500 requests per project per day.** Three DeepEval gate runs (~100 judge calls each,
because faithfulness fans out one call per extracted claim) plus a 128-job Ragas pass
reaches it. Pacing cannot help: the limiter's whole job is to spread requests over minutes,
and this budget is spent over a day.

The nastiest detail is that the 429 body says **"Please retry in 43.831872857s"** for the
daily cap too. That hint is correct for the per-minute quota and actively wrong for this
one — `_retry_after` parsed and honoured it, so the client burned all six attempts in four
minutes and then raised "judge rate limited... lower rpm", pointing at the wrong quota.

Fixed by discriminating the two, since they arrive as the same status code with the same
hint and demand opposite responses:

  - per-minute → pace and retry, as before;
  - per-day → raise `DailyQuotaExhausted` immediately, naming the model, stating that
    retrying cannot help before the reset, and pointing at `.cache/ragas` — because Ragas
    keys its cache on the prompt, a resumed pass replays already-scored rows for free and
    only pays for what failed.

Covered by `tests/test_judge_quota.py`, which is string-matching by necessity: the client
puts the quota id in the exception text rather than in a structured field, so the parser is
the contract worth pinning.

Budgeting lesson for the remaining days: a full Ragas pass and a gate run cannot share a
day on one free key. Run the gate against a cached judge, or keep the two on separate keys.

### The judge cache could not tell two judges apart

Found while checking whether tomorrow's resumed Ragas pass would safely reuse the cache.
It would — but only because the judge has not changed. `ragas.cache._generate_cache_key`
builds its key like this:

```python
if inspect.ismethod(func):
    args = args[1:]          # drops `self`
```

`self` is the LLM wrapper, and the wrapper is the only thing carrying the model. So the
key is (function qualname, prompt, kwargs) and **the model is not in it**. Two different
judges asked an identical question hash to one entry — confirmed directly: keys for
`gemini-3.1-flash-lite` and `gemini-3.5-flash` on the same prompt are byte-identical.

This is fine for the case the cache was written for and wrong for the case this project
keeps hitting. Three judge models were tried and rejected before the current one, and each
of those switches would have replayed the previous judge's verdicts while
`ragas_runner.py` wrote the *new* model's name into the output as `judge_model`. A
mislabelled artifact is worse than a slow one — a slow run announces itself, and a number
attributed to the wrong judge does not.

Fixed by partitioning the cache directory per model (`.cache/ragas/<judge_model>/`), which
makes a model switch a guaranteed miss and keeps each judge's entries reusable. Cruder than
fixing the key, but it works against the library as shipped instead of depending on a
private function's behaviour staying put.

The 242 entries already on disk were migrated into the `gemini-3.1-flash-lite` namespace
rather than discarded — they are all from that model, and they represent roughly half a
day's quota.

`tests/test_ragas_cache.py` pins both halves. The collision test asserts that the keys
*do* collide, so that if a future ragas release starts including the model, the test fails
and says the directory split is now redundant — rather than leaving behind a workaround
nobody remembers the reason for.

### MPS embedding throughput collapses 38x over a long encode

The `filings_sentence_window` index (190,858 chunks) was budgeted at ~3.5 hours by the
build guide and deferred for days on that basis. It actually takes **12m39s**. The gap was
one line of missing cache management.

Measured on this corpus at a fixed `batch_size=128`, in 5,000-chunk windows:

    432, 465, 459, 309, 137, 12 chunks/s     <- one uninterrupted encode
    398, 440, 442, 426, 333, 395 chunks/s    <- torch.mps.empty_cache() between windows

Throughput decays as the MPS allocator's cache grows, reaching a 38x collapse by 30k
chunks. Allocated memory stays flat at 133 MB when flushed, so this is allocator
behaviour, not the model.

The trap is the first hypothesis. Benchmarking batch sizes 128/256/512 gave 445/21/10
chunks/s, which reads as an obvious batch-size cliff and would have been written up as
one. It was a confound: those three runs happened in that order *in the same process*, so
each later batch size was measured against an already-degraded allocator. Re-testing at a
fixed batch size showed the same decay curve with no batch-size change at all. A benchmark
whose runs share mutable process state is measuring the order, not the variable.

Fixed in `src/finhelm/embeddings.py`: `_encode_all()` blocks the encode at
`MPS_FLUSH_EVERY = 8192` and calls `torch.mps.empty_cache()` between blocks. CUDA and CPU
take the single-call path unchanged — neither shows the decay, and blocking there would
only add overhead and fragment the progress bar.

Blocked and unblocked outputs are not bit-identical (max abs difference 2.086e-07) because
the reduction order differs, but that is at float32 epsilon (1.19e-07); rows remain
unit-norm and cosine ranking is unaffected.

### sentence_window was scored through the entire ablation without its windows

`chunking/sentence_window.py` states the design plainly — index one sentence for embedding
precision, splice the ±3 neighbours back in so the reader sees coherent context — and
ships an `expand()` that does it. Nothing ever called it. Not retrieval, not generation,
not the eval harness.

So every sentence_window cell in the ablation was scored on isolated sentences averaging
213 characters, against a `fixed` row whose chunks average ~4,566. The recall metric asks
whether a retrieved chunk contains a contiguous 10-word run of the gold span; the
sentence_window row was answering that question with about a ninth of the text width. It
lost, and the loss looked like a finding about chunking.

Correcting it moves the row up across the board — 12 hits gained, 0 lost:

    cell                        before   after
    sentence_window-dense        0.204   0.241
    sentence_window-hybrid       0.259   0.278
    sentence_window-dense-rr     0.194   0.232
    sentence_window-hybrid-rr    0.269   0.315
    sentence_window-bm25-rr      0.269   0.296

The conclusion survives — `semantic-hybrid-rr` still wins at 0.389 — but it survives on a
fair comparison now instead of a rigged one, and the margin over sentence_window is 0.074
rather than the 0.120 originally recorded.

**The near-miss is the part worth keeping.** The first attempt at measuring the impact
said expansion made recall *worse* (0.269 -> 0.222), which is impossible: a window
contains its own sentence, so it can only add n-gram matches. That impossibility was the
only reason it got a second look. The cause was in the measurement script — it keyed the
sentence list on `doc_id`, but `chunk_doc()` runs per *(document, section)*, so for the 15
of 460 filings carrying more than one section the sentence lists were interleaved and the
window was sliced out of the wrong section. It produced real sentences from the real
filing, just the wrong ones, and raised nothing. Had the sign come out merely small
instead of negative, it would have been believed.

Fixed in `src/finhelm/retrieve/window.py`, which expands *after* reranking so that both
the bi-encoder and the cross-encoder still score the bare sentence — that precision is the
entire reason to use this strategy. Expansion is keyed per hit, so a result set that mixes
`filings` (sentence_window) with `complaints` (fixed fallback) needs no special casing.

`tests/test_sentence_window_expansion.py` pins the section-keying bug, the
must-not-mutate-BM25's-cached-records constraint, and the monotonicity of `is_hit` under
expansion — the last because `_contradicts()` is a genuinely non-monotonic path (a window
drags in neighbouring figures the lone sentence did not have), so "expansion cannot lose a
hit" is an assumption that deserves a test rather than an argument.

### Failure taxonomy (2.9) — `semantic-hybrid-rr`, 54 answerable + 21 negative

Produced by `scripts/failure_taxonomy.py`, which is a script and not a tally in this file
because Day 3 exists to move these numbers. Each failure is charged to its *earliest*
cause — routing, then retrieval, then the abstention decision, then synthesis — because
the categories overlap and independent counts produce a table that sums past 100% and
cannot be used to prioritise.

    correct                           22  (41%)   single_hop 18, multi_hop 2, temporal 2
    retrieval miss (abstained)        19  (35%)   single_hop 8, multi_hop 8, temporal 3
    retrieval miss (answered anyway)  10  (19%)   single_hop 7, multi_hop 2, temporal 1
    over-refusal                       2  (4%)    temporal 2
    routing error                      1  (2%)    single_hop 1

    hallucinated on a negative         2  (10% of 21)   q055, q075

**Retrieval is the whole problem: 29 of 54, 54%.** Everything else is a rounding error
next to it. Day 3 should spend its budget on recall — query expansion, better fusion, more
candidates before rerank — and not on the generator or the abstention threshold.

Three things this separation makes visible that the aggregate metrics hide:

*`over_refusal_rate` reads 0.407 and the real figure is 2.* Twenty-two positives abstained,
which looks like a badly-tuned abstention threshold. But 20 of those 22 abstained because
retrieval returned nothing — that is the system declining to invent an answer, which is the
behaviour the negatives exist to reward. Only 2 refused while actually holding the gold
span. Tuning the threshold on the 0.407 figure would trade the system's one genuinely good
property against a problem it does not have.

*Synthesis is not a failure mode here — 0 cases.* Of the 13 answers where the gold span
quantified something and the system both retrieved it and answered, all 13 carried at
least one gold figure. Stated as a floor rather than a verdict, since the check is figure
agreement and not a judge, but the direction is unambiguous: when this system has the right
chunk, it uses it.

*The 10 that answered without the gold span are the dangerous row.* They are not refusals
and not synthesis errors; they are confident answers built on the wrong evidence, and no
aggregate in the harness isolates them. `recall@5` counts them as misses and
`over_refusal_rate` ignores them entirely.

**Temporal is the weakest question type: 6 of 8 fail** (q047, q049, q050, q051, q053,
q054), and 5 of those 6 are retrieval misses rather than date confusion — the comparison
spans two filings and retrieval surfaces at most one. Multi-hop is nearly as bad: 10 of 12
fail, again dominated by retrieval. Both are the same underlying shortfall, that a single
query embedding cannot fetch both halves of a comparison, which is the argument for the
Day 4 decomposition step.

Counting method and its limits are documented in the script's docstring. The one worth
repeating: `misrouted()` compares `set(expected_source) & set(route)`. An earlier ad-hoc
version wrote `expected_source not in route`, comparing a list against list *members*, and
reported 67 of 75 misrouted — a number that contradicted the harness's own route_accuracy
of 0.955 and was believed for several minutes anyway.

### Every MLflow run in the project was logged to macOS AirPlay

Spec 2.5 is the one that "turns *I tried some things* into *I ran a controlled
experiment*". For all of Day 2 it recorded nothing.

`MLFLOW_TRACKING_URI` was `http://localhost:5000` with no MLflow server behind it. On
macOS, port 5000 belongs to Control Center's AirPlay receiver, which is listening, and
which answers an unknown POST with **403 Forbidden**:

    $ curl -i http://localhost:5000/api/2.0/mlflow/experiments/get-by-name
    HTTP/1.1 403 Forbidden
    Server: AirTunes/935.7.1

That is the entire reason this survived two days. A missing server gives
`ConnectionRefusedError`, which reads as "nothing is there" and gets fixed in a minute. A
403 reads as "the server is there and is rejecting me", which reads as an auth problem
with something that exists — and the handler printed it as one dim parenthetical:

    (mlflow logging failed, result still saved: ... error code 403 != 200)

Eighteen ablation cells printed that line. It scrolled past under seventy-five lines of
per-question progress every time.

Two failures, and the second is mine rather than Apple's:

*Catching the exception was right; whispering was not.* The comment on the handler says
tracking must never fail a paid eval run, and that is correct — losing a $1.19 run to a
telemetry hiccup would be worse. But "non-fatal" was implemented as "invisible". The
handler is now loud, prints the resolved tracking URI, and names the recovery script.

*The recovery script initially wrote to a different store than the harness.*
`backfill_mlflow.py` did not import anything that loads `.env`, so it fell back to a stray
`sqlite:///mlflow.db` while `run_eval.py` used the dotenv value. Two tracking stores that
each look healthy in isolation is worse than none, because the ablation then appears to be
*missing runs* rather than appearing to be broken. Fixed by importing the dotenv loader for
its side effect.

Nothing was lost: `evals/results/<run>.json` holds config, summary and every per-question
record, which is a superset of what gets logged, so all 20 runs were reconstructed from
disk rather than by re-running a sweep that cost real money. Backfilled runs are tagged
`backfilled=true` with the result file's mtime as `ran_at`, because MLflow stamps them with
the moment they were written and the true ordering is the part worth keeping.

Two smaller things fell out of the same investigation:

- MLflow 3 refuses a file store outright (`./mlruns` is "in maintenance mode"), so the
  documented `file:` URI is no longer usable; the store is now `sqlite:///mlflow.db`.
- `log_mlflow` named runs with `cfg.run_name()` while the result file used the `--tag`
  suffix, so a 5-question `--tag smoke` run landed in the experiment under the *same name*
  as the real 75-question cell. Now both use the tagged name.

## Day 2.5: three predicted wins that the data refused to confirm

The Day 2 post-mortem produced a plan with a ranked set of fixes. Measuring them changed
the ranking, and in two cases inverted it. Recording the predictions next to the outcomes,
because the pattern — plausible mechanism, measurable, and wrong — is the point.

### Prediction 1: "pool starvation is the dominant lever". Wrong.

Instrumenting the pre-rerank candidate pool showed pool recall of 0.4459 at
`top_k_retrieve=20`, rising to 0.6351 at 100 and 0.7432 at 200, against a final recall@5
of 0.389. The pipeline after the pool was losing only ~0.06, so widening it looked like
the obvious win and the plan led with it.

Sweeping it measured:

    k=20   recall@5 0.4167   p50 1351 ms
    k=50   recall@5 0.3981   p50 3563 ms
    k=100  recall@5 0.4352   p50 9694 ms

None of the pairwise differences is distinguishable (paired bootstrap over 54 questions,
every interval spanning 0), and k=50 is nominally *worse* than k=20. Latency grows 7x.

The pool was never the binding constraint. Feeding the cross-encoder three to five times
more candidates hands it more distractors and it does not find more gold. The position
data says why: when reranking works it puts the gold chunk at **rank 1** (14 of 27 hits at
k=20), and recall@8 is barely above recall@5 (0.365 vs 0.351). The reranker is not nearly
right and short of context — it is binary. Either it recognises the passage or it does not,
and pool width does not change which.

Corollary worth keeping: **pool recall is a ceiling, not a forecast.** It bounds what the
selector could retrieve, and says nothing about what it will.

### Prediction 2: "contextual headers are the highest expected value change". Not shown.

Chunks carry no issuer, form, period or section, and 48% of missed spans came from a
document retrieval had already surfaced — so prepending that metadata before embedding
should separate near-identical filings. Both indexes were built (`*_ctx`, kept beside the
originals so the comparison stays an A/B rather than a one-way door).

    plain        recall@5 0.4167   mrr 0.3293   p50 1351 ms
    contextual   recall@5 0.4352   mrr 0.3744   p50 1809 ms

    paired bootstrap: +0.0185, 95% CI [-0.0185, +0.0648], P(better) 0.709
    2 questions gained, 1 lost

Not distinguishable. MRR moved more than recall, which is consistent with headers helping
rank an already-retrieved passage rather than retrieving a new one — but on 54 questions
that reading is a hypothesis, not a result. Kept off by default; the flag and the index
both survive so it can be re-tested on a larger set.

### Prediction 3: "the BGE query prefix is minor". Also wrong, in the other direction.

`bge-small-en-v1.5` is trained asymmetrically and `encode()` embedded queries exactly like
passages for all of Day 2. Dismissed in the plan as worth "~3 spans". Measured at the same
config, prefix off vs on: 0.3889 -> 0.4167, +0.0278, 95% CI [+0.0000, +0.0741], 2 gained,
**0 lost**.

Still not distinguishable at this sample size — but it is the same magnitude as the two
changes that were predicted to be large, it never loses a question, and it costs nothing.
The ranking of "big" and "small" fixes was not supported by any measurement when it was
written.

### The finding underneath all three

Every change measured this session lands between +0.018 and +0.028 with a 95% interval
spanning roughly +/-0.06. That is not a coincidence about the changes; it is the resolution
limit of a golden set with 74 gold spans. At p ~ 0.4 the Wilson interval on the headline
number is [0.252, 0.465] — wide enough to contain the top six rows of the Day 2 ablation.

**The 18-cell ablation could not distinguish its own top six configurations**, and neither
can any of this work. Reporting a winner from it was over-claiming.

Both are now reported rather than left implicit: `wilson()` and `bootstrap_paired()` in
`evals/metrics.py`, a CI column in the ablation table, and `n_gold_spans` in every summary.
The paired bootstrap is the one to use for comparisons — both configs answer identical
questions, so pairing removes the question-difficulty variance that dominates the
independent intervals.

### What actually needs to happen next

Not more tuning. The set has to get bigger before any tuning result can be read. 127 new
questions (174 gold spans) are drafted and mechanically verified in
`evals/golden_expansion_unreviewed.jsonl` — deliberately NOT merged into
`golden_set.jsonl`, because `scripts/verify_golden.py` checks findability, triviality and
duplication, and none of those is a human deciding whether a question is well-posed.

### Temporal questions: the plan's fix was the wrong fix

The plan proposed metadata date-filtering for the 8 temporal questions (6 of which fail).
Reading them first: **every one spans two documents from different periods** — two gold
docs, years apart, in all 8. A date filter would guarantee missing half of every one of
them. They are structurally multi-hop, so decomposition is the right mechanism and the
filter idea was dropped before being built.

### Operational: how to run a long job here without losing it

Four separate ways a job "started and nothing happened", all in one session:

  * `nohup ... &` detaches, so the harness reports the *launcher* finished while the real
    process runs on invisibly. One such orphan ran 30 minutes and poisoned every GPU
    timing taken beside it.
  * Piping a build through `grep` block-buffers its output to a file, so a job that is
    working looks identical to one that is wedged.
  * Killing an MPS job can leave the next one crawling at ~4% speed — 32 seconds of CPU in
    14 minutes. A clean restart embedded the same chunks in under a minute.
  * FAISS and a second CrossEncoder in one process aborts in native code with no traceback
    and a leaked-semaphore warning. The real pipeline never does this; benchmark scripts do.

What works: `python -u` writing straight to a log, harness-tracked, no pipes, and progress
verified by log growth rather than by `%cpu` — which understates MPS work badly enough to
read as stalled.

## Question design was not the problem; span count was (Day 2 audit)

The hypothesis was that LLM-drafted questions were too long and compound, so that low
recall partly measured question style rather than retrieval. The first cut supported it:
recall by question-length tertile ran 0.556 / 0.500 / 0.194, a 2.9x spread.

Controlling for question type destroyed it. *Within* each type the length effect is gone:
multi_hop +0.000, single_hop -0.059, temporal +0.375 — the last in the wrong direction
entirely. Length was proxying for something else.

    1 gold span   n=34   recall 0.559
    2 gold spans  n=20   recall 0.175

That 3.2x gap is the whole finding. It is not about phrasing. All 20 two-span questions
span two different documents, and 70% of them retrieve *neither* side — only 5% get both.

The crowding explanation is also wrong. When one side is found, its document holds 1.00 of
the 5 context slots, not 4 — the other slots go to documents that are neither target. A
single query vector lands on generic vocabulary matches instead of either specific filing.

Corollary for the metric: pooled recall says as much about the *mix* of question types in
the golden set as about the retriever. Adding ten single-span questions raises "recall"
with no code change. `recall_by_span_count` in evals/metrics.py splits it so a change that
helps comparisons is visible even when it moves the pooled average by nothing.

## Decomposition works; the instrumentation measuring it did not

First agentic run reported "decomposition fired on 0/54 questions" while cost per query had
gone from $0.0001 to $0.0007 and p50 latency from 1610ms to 3525ms. Both facts cannot be
true. `run_eval`'s retrieve-only branch never recorded `sub_questions`, so the check was
reading a field that is always empty — the same class of error as scoring the pool on
doc_id and calling it span presence.

Probed directly, decompose splits 8/8 multi-span questions and the splits are good: each
names company, metric and period explicitly. It now fires on 53/54 and is recorded.

Two hypotheses died here:
  - "rerank undoes decomposition by re-scoring against the original compound question" —
    false. agentic+rerank scores 0.225 on multi-span against agentic-no-rerank's 0.200,
    and rerank is worth +0.157 pooled. Rerank is the single most valuable component
    measured so far.
  - "a wider candidate pool recovers the missing side" — false, measured earlier.

## The real finding: every experiment on this golden set is underpowered

Paired multi-span differences have sd 0.35 at n=20. Questions needed to resolve an effect
with 95% confidence and 80% power:

    effect 0.20 ->   25 multi-span questions
    effect 0.15 ->   43
    effect 0.10 ->   97
    effect 0.05 ->  385     <- the size of every effect measured so far

There are 20. Nothing measured this session — query prefix, contextual headers, pool width,
decomposition — produced an effect larger than 0.05, and none of them could have been
resolved if it had. The Day 2 ablation ranked 18 cells on gaps smaller than this.

The actionable form: ~45 multi-span questions makes effects of 0.15+ resolvable, which is
a realistic authoring target. Chasing 0.05 effects needs 385 and is not worth it. So the
strategy is to stop tuning for small gains and look for a large one — the untested
candidate being the embedding model, since bge-small-en-v1.5 has 384 dimensions to
separate ten banks' near-identical MD&A prose.

## I measured a bigger context budget and called it a finding

Two-span questions retrieve neither side 70% of the time. The hypothesis was that
decomposition already finds both sides and the shared top-5 discards one, so an isolation
test gave each sub-question "its own top-5":

    gold spans found in the joint top-5                : 7/40  (17.5%)
    gold spans found in some sub-question's own top-5  : 14/40 (35.0%)

A doubling, 3.5x larger than any other effect measured. It was invalid. A two-way split
gets 10 slots that way and a four-way split gets 20, against 5 for the baseline. The test
varied context budget and query shape together and attributed the result to query shape.

Implemented properly — round-robin quota at an equal 5-slot budget, each pool reranked
against its own sub-question — it loses on every tier:

                        all     single   multi
    baseline          0.4167    0.5588   0.1750
    agentic (fused)   0.4352    0.5588   0.2250
    agentic (alloc)   0.3611    0.4706   0.1750     paired vs baseline: -0.0556 [-0.139, +0.009]

Reverted. Two things worth keeping from it:

  - Reranking the fused pool against the *original* question is not the mistake it looked
    like. It beats per-sub-question reranking, and rerank remains the single most valuable
    component measured (+0.157 pooled).
  - decompose splits 53 of 54 questions, including single-fact ones. That is why the
    allocation hurt single-span questions worst — it dilutes a budget across sub-questions
    that were never needed. Any future work here has to gate on the split being warranted,
    which is a cheaper and more promising change than anything tried today: decomposition
    is currently paying 2.9x latency and 7x cost on every question to help a fifth of them.

The pattern this repeats — verified three times this session — is that a plausible number
with no exception attached is the most dangerous output this project produces. doc_id
mistaken for span presence, an empty sub_questions field read as "decomposition never
ran", and now budget mistaken for query shape. All three produced believable numbers.

## Gating decomposition: identical recall, half the cost and latency

decompose was splitting 53 of 54 answerable questions, including single-fact ones it
"split" into near-paraphrases. A deterministic pre-check now runs first — two issuers
named, comparative phrasing, or two distinct periods — and only then is the model asked.

    config                    all    single   multi    p50 ms   $/query
    baseline (no agentic)   0.4167   0.5588  0.1750      1351    0.0001
    agentic ungated         0.4352   0.5588  0.2250      3525    0.0007
    agentic GATED           0.4352   0.5588  0.2250      1782    0.0004

Paired delta against ungated is exactly 0.0000 on every tier, CI [0.0000, 0.0000]: not
"too small to resolve" but literally the same retrieved sets. 49% faster, 49% cheaper,
20/20 multi-span coverage retained.

This is the first unambiguous win of the audit, and the reason is methodological rather
than lucky. Recall on this golden set has a resolution floor of about 0.15; cost and
latency have almost no variance at all. Optimising the thing you can actually measure
beats optimising the thing you care about but cannot resolve — and here they did not
conflict, because the question was never "does decomposition help?" but "does it need to
run every time?"

Two bugs fell out of building it, both found by tests written against the gate:

  * The router's `_COMPARATIVE` pattern matched compare/compared but not *comparing*,
    *comparison* or *contrasting*. The participle fails the trailing (?!\w) boundary and
    the noun form was simply absent. This was never a decomposition bug — that regex also
    forces two-collection routing, so "Comparing X with Y" was being routed one-sided.
    Fixing it lifted gate coverage from 18/20 multi-span to 20/20.
  * Deriving issuer aliases from company names manufactures generic finance vocabulary:
    "Capital One" yields "capital", "Synchrony Financial" yields "financial", "American
    Express" yields "american" and "express". "What are the bank's capital requirements?"
    then reads as naming an issuer. Replaced with an explicit alias table plus
    case-sensitive ticker matching, since "C" is Citigroup and also the most common lone
    letter in a lowercased finance question. Single-span false positives halved, 15 -> 7.

## The embedding-model test is set up but not run (environment, not code)

Index identity now includes the embedding model, so `bge-base` builds alongside `bge-small`
rather than overwriting it — vectors from two models are not comparable, and if their
dimensionality happened to match, loading the wrong one would return confident nonsense
instead of raising.

    FINHELM_DEVICE=cpu .venv/bin/python scripts/build_index.py \
        --collection filings --strategy semantic --embed-model BAAI/bge-base-en-v1.5

Three attempts did not finish:

  * MPS, batch 128 — process alive in uninterruptible wait (state U), 2 minutes of CPU
    over 10 minutes of wall clock.
  * MPS, batch 64 — same, 23 seconds of CPU over 8 minutes.
  * CPU, batch 32 — genuinely progressing (state R, CPU time accruing at ~1:1) but the
    measured rate is 7.2 s per 32-chunk batch over 771 batches, i.e. ~85 minutes.

The MPS degradation is environment state, not a code fault: the contextual-header index
built normally on MPS earlier in the same session, and the machine only started wedging
after several embedding processes had been killed mid-run. A fresh process on a clean GPU
state should take 2-3 minutes. Diagnosis note for next time: `ps -Ao pid,time,stat` on the
*python* process, not the zsh wrapper — the wrapper always reads 0:00.00 and 0MB RSS, which
looks exactly like a dead process and led to one wrong "wedged" call here. State U with
flat CPU time is the wedge; state R with accruing CPU time is merely slow.

This remains the only untested candidate for an effect large enough to clear the golden
set's ~0.15 resolution floor.

## Reviewing the 127 drafted questions

Mechanical checks first, since they are cheap and rule out whole classes of problem:

  * All 174 gold spans are findable in the corpus (ceiling 1.0000). Partly circular — the
    questions were drafted *from* chunks — so this verifies the pipeline preserved the
    spans, not that the questions are good.
  * No lexical leakage. Question/snippet content overlap is 0.286 median against 0.321 for
    the existing set, and long shared runs appear in 11% of spans against 18%. The drafts
    are marginally *harder* than what they join, not easier.
  * No dangling references to the source text, no empty ground truth, no type/span
    mismatches, no duplicate or near-duplicate questions.
  * **75 of the 127 ids collided with existing ids.** The drafts restart numbering at q001,
    so q001-q075 named entirely different questions from the ones already in the set.
    Result files are keyed by question id, so merging as-is would not have errored — it
    would have silently rewritten the meaning of every historical per-question comparison.
    Renumbered to q076-q202.

Reading them surfaced one real validity problem: some multi-hop questions yoke two
arbitrary facts. q081 asked how DFS defines tangible common equity *and* how AXP defines
reserve build — unrelated metrics no analyst would pair. q083 compared "the scale of" a
$40bn buyback against a $1.1bn FDIC accrual, which is not a comparison.

Quantified by content overlap between the two gold snippets, pairs sharing under 10%:

    existing golden set   9/20  (45%)
    new drafts           25/47  (53%)

So this is not a defect the drafts introduce — it is how the whole benchmark was generated,
and it partly explains the 0.175 multi-span recall: half of those questions have no shared
vocabulary linking the two spans, so no single query can retrieve both. Worth recording as
a limitation of the benchmark rather than a reason to reject the drafts, which are
consistent with what they join. Two questions carrying a trailing speculative clause the
spans cannot support ("what does this suggest about the yield environment?") had the clause
trimmed rather than being discarded, since their factual cores are verified.

Merged: 202 questions, 248 gold spans, 67 multi-span (was 74 and 20). Ceiling stays 1.0000.

## The span-count finding replicates on 3.4x the data

                        n=74 spans    n=248 spans
    single-span           0.5588         0.5439      (n 34 -> 114)
    multi-span            0.1750         0.1716      (n 20 ->  67)
    macro recall@5        0.4167         0.4061
    CI width              0.213          0.117

Both tiers land within 0.015 of their original values on 3.4x the questions. The 3.2x gap
between single- and multi-span retrieval is now the best-established fact in this project.

Resolvable effect size improves from ~0.15 to ~0.12, and 67 multi-span questions is past
the 43 needed to resolve 0.15.

## A point estimate outside its own confidence interval

The expanded run reported recall_at_5 = 0.4061 next to a CI of [0.2865, 0.4038]. The point
estimate sat outside its own interval, which should be impossible.

The two numbers described different quantities. `recall_at_5` is a *macro* average — the
mean of per-question recall, weighting a two-span question the same as a one-span one —
while the Wilson interval was computed on the *micro* rate, spans-found over spans-total.
They coincide only when every question carries the same number of spans, which was nearly
true at n=74 and stopped being true once 47 two-span questions were merged.

Fixed by giving the macro estimate a bootstrap over questions, which is its actual sampling
distribution, and reporting the micro rate with its Wilson interval alongside. Both are
informative — macro is "how does the system do on an average question", micro is "what
fraction of the evidence does it retrieve" — and the gap between them (0.406 vs 0.343)
measures how much of the difficulty is concentrated in multi-span questions.

A number outside its own interval is worse than reporting no interval at all, because it
still reads as authoritative.

## End-to-end on 202 questions, and the first effect the golden set could resolve

Full pipeline (semantic + hybrid + rerank + gated decomposition), generation included:

    macro recall@5   0.4171   CI [0.351, 0.483]      micro 0.3589
    citation_validity 1.0000                          abstention_recall 0.9048
    citation_density  0.8822                          over_refusal_rate 0.3646 (misleading, see below)
    p50 latency       8.6 s                           cost $0.0172/query

Failure taxonomy over the 181 answerable questions, charged to earliest cause:

    correct                          71  (39%)
    retrieval miss (abstained)       46  (25%)
    retrieval miss (answered anyway) 42  (23%)
    over-refusal                     14  ( 8%)
    routing error                     6  ( 3%)
    wrong synthesis                   2  ( 1%)

**Retrieval is 48% of all questions and effectively all of the failure.** Everything
downstream of it is in good shape: citation validity is 1.0000 across 202 questions (no
fabricated source markers at all), synthesis is wrong on 2 of 42 checkable cases, and 19
of 21 negatives are correctly refused.

`over_refusal_rate` reads 0.3646 and again does not mean what it says: only 14 of the 87
questions that actually held the gold span were refused. The rest are the system correctly
declining to answer from evidence it never retrieved. That is the right behaviour and
should not be tuned away — the abstention threshold is not the problem, recall is.

Two negatives still get confident fabricated answers, q055 and q075 — the same two as
Day 2, and q055 remains the deliberate strict xfail in the PR gate.

### Contextual headers: rejected at n=74, resolved at n=248

                        all      single-span   multi-span
    plain index       0.4171       0.5439        0.2015
    contextual index  0.4475       0.5877        0.2090

    paired, all          +0.0304  [+0.0028, +0.0635]  p=0.977   <- excludes zero
    paired, single-span  +0.0439  [+0.0000, +0.0877]  p=0.970
    paired, multi-span   +0.0075  [-0.0224, +0.0373]  p=0.589

The identical change measured +0.0185 [-0.019, +0.065] on the old 74-span set and was
correctly recorded as unresolvable. Nothing about the change improved; the instrument did.
This is the clearest possible argument for having spent the time on the golden set rather
than on more tuning, and it retroactively casts doubt on the other small effects rejected
earlier — the BGE prefix in particular is worth re-measuring at n=248.

The gain is concentrated in single-span questions, which fits the mechanism: a header names
issuer, form, period and section, so it disambiguates *which* filing a passage came from.
It does little for multi-span, where the difficulty is needing two documents at once.

Left off by default despite winning. Only `filings/semantic` and `complaints/fixed` have a
`_ctx` index built, which covers the winning config exactly but nothing else; defaulting it
on would make `--chunking fixed` and `--chunking sentence_window` fail on a missing index
instead of falling back. Build the remaining `_ctx` indexes before flipping the default.

## bge-base: the first change that helps the tier that was actually broken

Rebuilt the contextual indexes with `BAAI/bge-base-en-v1.5` (768-dim) against
`bge-small-en-v1.5` (384-dim), everything else held fixed.

                        all      single-span   multi-span
    bge-small, plain  0.4171       0.5439        0.2015
    bge-small + ctx   0.4475       0.5877        0.2090
    bge-base  + ctx   0.4558       0.5789        0.2463

    ctx headers   (small -> small+ctx)  single +0.0439 [+0.0000,+0.0877] RESOLVED
                                        multi  +0.0075 [-0.0224,+0.0373]
    bigger model  (small+ctx -> base)   single -0.0088 [-0.0526,+0.0351]
                                        multi  +0.0373 [+0.0075,+0.0746] RESOLVED
    both          (plain -> base+ctx)   all    +0.0387 [+0.0028,+0.0773] RESOLVED

The two interventions are complementary rather than competing, and each moves exactly the
tier its mechanism predicts. A contextual header names issuer, form, period and section, so
it disambiguates *which* filing a passage came from — that is a single-span problem, and it
does nothing measurable for multi-span. A larger embedding model gives finer discrimination
between the near-identical passages that two-document comparisons have to separate, and it
does nothing for single-span, where the header had already resolved the ambiguity.

Reporting only the pooled column would have hidden both: +0.0083 for the model swap, well
inside noise, and the conclusion "bigger model does not help" would have been wrong.

Cost is real but tolerable: p50 retrieval 3932ms -> 4440ms, and the index build is 22
chunks/s against 90 for bge-small (19 min for filings, 25 for complaints, against ~8
combined). bge-large was ruled out on this hardware at ~75 min for the same corpus.

### MPS decay, second occurrence

The first bge-base build passed 15 minutes without reaching the 8192-chunk checkpoint that
`bge-small` clears in under a minute, on a verified-clean GPU — so this was not leftover
state from killed processes, which had been the explanation the first time. Isolated
throughput measurement:

    bge-small  mps  90.3 chunks/s     bge-small  cpu  21.8 chunks/s
    bge-base   mps  29.4 chunks/s     bge-base   cpu   9.0 chunks/s

MPS is the correct device and bge-base is genuinely ~3x the work. But 29.4 chunks/s
predicts 8192 in 4.6 minutes, and the real build was far past that — so decay was still
happening *inside* a single flush window. `MPS_FLUSH_EVERY` is a memory budget rather than
a count, and a 768-dim model puts roughly twice the allocator pressure through the same
window. Lowering it 8192 -> 2048 held a steady 22 chunks/s across the entire 43k-chunk
build with no collapse.

## The architecture, not the model — and I aimed the first fix at the wrong stage

Tripling the embedding model moved the pooled number by +0.008, which is the shape of a
result where something downstream is discarding what the model improved. It was, but not
where I first said.

### What is genuinely broken: RRF across sub-questions

`hybrid.fuse` scores `1/(rrf_k + rank)` summed across pools. With rrf_k=60 on 20-item
lists the spread across an entire list is 1.31x, while each extra pool adds a full
increment:

    rank  1 in one pool    0.016393
    rank 20 in two pools   0.025000   <- wins

A chunk ranked last in two pools outranks a chunk ranked first in one. For a comparison
that is backwards: the passage answering the AXP half is rank 1 in the AXP sub-question's
pool and absent from the JPM one, while a generic passage mediocre in both appears twice
and wins. Traced on six multi-span questions, all 8 of every fused top-8 appeared in more
than one pool, and gold chunks at rank 6, 15 and 17 within a single pool emerged at 36, 42
and 52 after fusion. rrf_k=60 comes from TREC, where lists are hundreds long and there are
two of them; it is the wrong constant for five 20-item pools.

### Why fixing it changed almost nothing

Interleaving instead of fusing: +0.0028 pooled, +0.0075 multi-span, one question of 67
changed. Not resolved.

The demotion is real and its consequence is absorbed. The candidate pool is
`top_k_retrieve * n_pools` = 60 wide, so a chunk demoted to rank 42 still arrives, and the
reranker rescores the whole pool anyway. Fusion order barely reaches the output. The
arithmetic was right and the conclusion drawn from it was wrong, because the analysis
stopped one stage short of where the decision is actually made.

### Where the evidence is actually lost

Multi-span gold spans, 134 of them across 67 questions:

    reach the final top-8                       31%
    in the candidate pool, dropped by rerank    33%   <- the reranker's doing
    never reach the candidate pool              37%

`rerank(query, candidates, ...)` scores every candidate against the *original compound
question*. Asked which passages best answer a string naming two facts, a cross-encoder
correctly prefers passages moderately about both — never the one that decisively answers
half. So a third of the gold spans that retrieval successfully found are discarded at the
last step, by a component doing exactly what it was asked to do.

`rerank_per_query` scores each pool against the sub-question that produced it and draws
quotas round-robin from each pool's own ranking. Built and wired behind
`--rerank-per-subquestion`; not yet measured.

This differs from the allocation attempt recorded above, which lost on every tier, in two
ways that matter: it splits the *rerank* stage rather than the context budget, and
decomposition is now gated by `worth_splitting`, so single-span questions never enter the
path. That gating is what caused the earlier damage.

### Metric alignment

`--k` now defaults to `cfg.top_k_context` rather than a fixed 5. The generator is handed 8
chunks, so reporting recall@5 understated the system by 3.6 points (0.4558 vs 0.4917) for
no reason, and the gap would have grown silently with any change to top_k_context.

## Issuer filtering: the largest confirmed gain, on the tier I did not predict

    config                       all    single-span   multi-span   p50 ret
    base (rerank vs original)  0.4917     0.6053        0.2985      4440 ms
    rerank per sub-question    0.4972     0.5965        0.3284      2995 ms
    + issuer filtering         0.5359     0.6667        0.3134      3082 ms

    paired, rerank-per-subq -> + filtering
      all          +0.0387  [+0.0055, +0.0746]  p=0.985   <- excludes zero
      single-span  +0.0702  [+0.0175, +0.1228]  p=0.996   <- excludes zero
      multi-span   -0.0149  [-0.0522, +0.0224]  p=0.155

Restricting a single-issuer sub-question to that issuer's filings is worth +0.0387 pooled,
the largest resolved effect measured on this project.

**The prediction behind it was wrong in an instructive way.** Filtering was proposed as the
strongest lever against *multi-span* failure, on the argument that each sub-question names
one issuer and one period. It does nothing for multi-span (-0.0149, not resolved) and
everything for single-span (+0.0702). The mechanism is duller than the argument: most
single-hop questions name exactly one issuer, so the filter removes roughly nine tenths of
the corpus as distractors before ranking begins. Comparisons keep both halves competing in
the pool regardless of how each half was retrieved, which is why the tier the argument was
aimed at is the one it left alone.

Two failed predictions in a row on the same tier — the RRF fix and this — is the useful
pattern. Multi-span recall has not moved beyond noise under any intervention: RRF
replacement +0.0075, per-sub-question rerank +0.0299 [-0.0373, +0.0970], filtering -0.0149.
Whatever makes a two-document question hard is not in the ranking or selection stages,
because changes to both have now been tried and neither moved it.

### Per-sub-question reranking: not resolved, but free latency

+0.0299 on multi-span with [-0.0373, +0.0970] — 11 questions improved, 8 regressed, which
is churn rather than a mechanism. The diagnosis that predicted it (33% of multi-span gold
spans sat in the pool and were dropped by a reranker scoring against the compound original)
was measured and correct; fixing the mismatch still did not convert those spans.

What did resolve is latency: p50 retrieval 4440 ms -> 2995 ms, a 33% cut, because five
pools of 20 are reranked separately rather than one merged pool of 60 being reranked whole.
Worth keeping for that alone, and it costs single-span 0.0088 which is inside noise.

### A silent process death, again — and it was never a code bug

The `--filter-by-issuer` run without per-sub-question reranking stopped at question 168 of
202 with no traceback, no error and no exit marker, and a retry died at question 43. I
recorded this as the "native-code crash seen when a FAISS store and a cross-encoder live in
the same process", which is the explanation this file had already reached for twice before.
It was wrong both times.

Running the same path in the foreground returned **exit code 137**. That is 128+9: SIGKILL.
Nothing crashed — macOS killed the process under memory pressure, which is why there was no
traceback to find. The absence of an error message was the evidence, and I read it as a
mysterious native fault rather than as the signature of a process that never got to handle
its own death.

Measured peak RSS through the identical path, both collections resident, six multi-hop
questions: **1697 MB, and flat.** On 8 GB that is not close to a limit. The config was never
the problem. What was: several memory-heavy Python processes of my own running at once —
an eval, an analysis script, a benchmark — on a machine with 8 GB shared between CPU and
MPS.

The operational rule is dull and would have saved two failed runs and a wrong diagnosis:
**check for stray processes before launching, and run one heavy job at a time.** The
earlier "FAISS and torch cannot share a process" conclusion should be treated as unproven;
every instance of it so far is explained by memory pressure instead.

### Final four-way, and per-sub-question reranking earns nothing

    config                       all    single-span   multi-span   p50 ret
    base                       0.4917     0.6053        0.2985      4440 ms
    rerank-per-subquestion     0.4972     0.5965        0.3284      2995 ms
    filter only                0.5387     0.6667        0.3209      3434 ms
    filter + rerank-per-subq   0.5359     0.6667        0.3134      3082 ms

    paired, base -> filter only
      all          +0.0470  [+0.0193, +0.0773]  p=1.000
      single-span  +0.0614  [+0.0175, +0.1053]  p=0.999
      multi-span   +0.0224  [+0.0000, +0.0522]  p=0.953

    paired, filter only -> + rerank-per-subquestion
      all          -0.0028  [-0.0276, +0.0221]  p=0.367

Issuer filtering alone is the best configuration measured. Adding per-sub-question
reranking on top of it is worth -0.0028: the two were aimed at the same evidence, and
filtering gets there first by never admitting the distractors that reranking was being
asked to sort back out.

`filter_by_issuer` is now on by default. Unlike contextual headers it depends on no
prebuilt artifact — `filters_for` returns None whenever a question names zero or several
issuers, so a corpus with no tickers simply never filters — and leaving a +0.047 effect
behind a flag is a footgun. The run-name suffix is inverted accordingly: `-noflt` marks
the control arm.

`rerank_per_subquestion` stays in the tree, off. It is the correct fix for a real and
measured mismatch, it is 13% faster than reranking a merged pool, and it earns nothing
once filtering is present. Worth re-testing if the corpus ever grows past what an issuer
filter can usefully narrow.

**Where this leaves the system**, recall@8 on 202 questions / 248 gold spans:

    Day 2 close          0.389   (recall@5, 74 spans, unresolvable interval)
    + expanded golden    0.4171
    + contextual headers 0.4475
    + bge-base           0.4917
    + issuer filtering   0.5387   single-span 0.6667, multi-span 0.3209

Single-span has moved from 0.5439 to 0.6667 and every step of that is a resolved effect.
Multi-span has moved from 0.2015 to 0.3209 and almost none of it is: the only intervention
that resolved there was the embedding model. Four separate attempts to fix multi-span in
the ranking and selection stages — RRF replacement, per-sub-question rerank, per-sub-
question filtering, and pool widening before that — have all returned noise. The remaining
difficulty is not in how candidates are ordered or chosen.

## Widening a filtered pool makes multi-span worse — and that is the finding

Day 2 rejected pool widening on an unfiltered pool, where k=50 admits distractors from
every issuer. With issuer filtering the pool is high-precision, so widening should admit
more of the *right* issuer's chunks. Measured beforehand: of the 44 multi-span gold spans
that never reach the pool, a filtered search puts 21 within k=50 and 31 within k=100.

    config          all      single-span   multi-span   p50 ret   p95 ret
    filter, k=20  0.5387       0.6667         0.3209     3434 ms   13131 ms
    filter, k=50  0.5276       0.6754         0.2761    12716 ms   34568 ms

    paired multi-span  -0.0448  [-0.0970, +0.0075]  p_better=0.032

Multi-span got *worse*, at 3.7x the latency. Adding candidates that are known to contain
gold spans reduced the number of gold spans in the final context.

That is not a null result, it is a diagnosis. Six interventions have now been aimed at
multi-span — pool widening (twice), RRF replacement, per-sub-question rerank, per-sub-
question filtering, per-sub-question budget allocation — and the only one that ever
resolved was changing the embedding model. Put together with two measurements from the
current config:

  * 35% of multi-span gold spans are in the candidate pool and dropped at selection;
  * the final top-8 already draws from 6-8 distinct documents on most questions, so
    document diversity is not the constraint and per-document quotas would do nothing;

the conclusion is that **the cross-encoder cannot discriminate**. For a comparison it
cannot separate the passage that decisively answers one half from a plausible passage that
answers neither, so enlarging its input enlarges its opportunity to be wrong. Every fix
tried so far has been about *which candidates it sees* or *in what order*, and none of
those can help a scorer that ranks the wrong thing highest.

`bge-reranker-base` is the one component of this pipeline that has never been changed,
while the embedder, the chunk text, the fusion, the query shape and the filter all have.
It is also worth +0.157 — the single most valuable component measured on Day 2 — which
made it look settled rather than unexamined.

## A bigger reranker does not help either — so it is not capacity

Offline comparison on the 67 multi-span questions, reranking the *recorded* candidate
pools so nothing but the scorer changes. The base model reproduces the live number exactly
(0.3209), which validates the harness.

    ceiling: gold span present in the pool     90/134 = 0.6716
    bge-reranker-base                          43/134 = 0.3209   108 ms/pair
    bge-reranker-large                         41/134 = 0.3060   765 ms/pair

Two thirds of multi-span gold spans are already in the candidate pool. Both rerankers
convert about half of them, and tripling the cross-encoder converts slightly fewer at seven
times the cost. The gap between 0.32 and 0.67 is not a capacity problem, so "use a stronger
reranker" — which the previous entry in this file recommended — is wrong.

Nor is it a scoring-target problem. `rerank_per_query` scores each pool against its own
sub-question, which removes the compound-query mismatch entirely, and reaches 0.3284: the
best multi-span figure measured, and still half the ceiling. Even taking a quota from each
sub-question's own filtered, reranked list leaves the gold chunk outside the top few of a
pool drawn from the correct issuer's filings.

That is the real statement of the problem, and it is narrower than anything tried so far:
**within one issuer's filings, ranked against a sub-question naming that issuer, metric and
period, the answer-bearing passage is not in the top few.** Every intervention to date has
worked on which candidates are present or how they are ordered relative to each other.
None of them changes what a candidate *is*.

`chunk_tokens` has never been swept. The Day 2 ablation varied chunking *strategy* —
fixed, semantic, sentence_window — at a fixed 800 tokens throughout, and 42% of all misses
are "right document, wrong passage", which is the signature of chunks too coarse to
separate one disclosure from the next. It is the only untested lever that changes the unit
being ranked rather than the ranking.
