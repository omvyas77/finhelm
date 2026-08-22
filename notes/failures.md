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
