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
