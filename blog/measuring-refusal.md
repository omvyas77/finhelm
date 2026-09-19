# The number that matters is what your RAG system does when retrieval fails

Most RAG write-ups report a retrieval score and stop. Mine is 0.7377, recall@16 over 202
questions, macro-averaged, 95% interval [0.683, 0.792]. It is the least interesting number
in the project.

The first version of this post was built around one failure. Asked for Jamie Dimon's 2025
compensation, a question I had written as having no answer in the corpus, the system
returned $43,000,000, itemised and cited to a JPMorgan 8-K. I called it a fabrication and
made it a permanent failing test.

It wasn't one. JPMorgan discloses the CEO's annual pay in an 8-K every January, that 8-K
is in the corpus, and the passage the system cited contains every figure it gave. The
system was right. My label was wrong.

This is the post rewritten around what the evaluation shows once that is fixed.

---

## The hallucination that wasn't

When I wrote the negatives, I reasoned that executive pay lives in the proxy statement,
the corpus holds only 10-Ks, 10-Qs and 8-Ks, so the question could not be answered. That
is reasoning about filing types. I never searched the corpus for the fact itself.

The test infrastructure made the mistake look more convincing, not less. When the CI tier
ran against a smaller fixture corpus that lacked the 8-K, the system abstained on the same
question, and the strict xfail flipped to XPASS. I read that as the failure being
corpus-dependent. The simpler reading was the right one: the system answered when the
evidence was there and declined when it wasn't, both times.

Searching the full chunk store for every one of my 21 hand-written negatives, rather than
the 16 passages retrieved for each, turned up a second. q063 asks how each FOMC participant
voted on the balance-sheet runoff change. I had labelled it unanswerable because I thought
named votes covered only the rate decision. The March 2025 statement lists every vote on
the action that slowed runoff, and the one dissent was specifically about runoff. The
system abstained on q063, making the same assumption I had.

Fixing the two labels is a re-score, not a re-run. The model outputs are untouched, and
the metrics are recomputed from them with the same code:

| | before | after |
|---|---|---|
| abstention recall | 0.9048 (19 of 21) | **0.9474** (18 of 19) |
| over-refusal rate | 0.1160 (21 of 181) | **0.1202** (22 of 183) |
| recall@16, macro | 0.7403 | **0.7377** |

Over-refusal went up because q063, which the system declined, is now answerable.

## What the system does when retrieval misses

Of the 183 answerable questions, there are
**31 where retrieval did not surface the evidence at all**: none of the gold passages
reached the model. The system abstained on 13 of them. It answered the other
**18 anyway**, **58% of the time**.

The first version of this post called those 18 "answers with nothing behind them". The
scorer supports that reading. It matches a retrieved passage to a gold one by document and
a shared ten-word run, and it found nothing. But q055 had just shown me that the scorer can
miss valid evidence, so I read all 18:

| what the answer was | questions |
|---|:--:|
| right, from a passage the scorer doesn't credit | 8 |
| a refusal, stated partway through the answer | 2 |
| grounded in real passages, but answering a nearby question | 7 |
| wrong | 1 |

The first row is mostly the same fact in a different filing. Goldman's downgrade-collateral
figures came from 10-Qs that restate the December comparative. The tax condition on the
Discover merger came from Capital One's 10-K, which states the same condition as
Discover's. The second row is the model writing `INSUFFICIENT_CONTEXT` halfway through an
answer. My refusal detector only checks how the answer starts. The third row is the real
weakness: every claim traces to a passage that was retrieved, but the passages were next to
the question rather than on it, like a neighbouring risk factor or a different complaint,
and the answer addressed what it had.

**None of the 18 contains a figure that is not in the passages it was given.** The
classification, with a note per question, is in
[`evals/zero_recall_review.jsonl`](../evals/zero_recall_review.jsonl).

So 58% is an accurate description of behaviour, since the system answers rather than
declines, but "nothing behind it" is not. The verified failures come to 8 of 183
answerable questions: seven partial answers and one wrong one.

## Citation validity is 1.0000, and it is not what it sounds like

Every citation marker in every answer points at a source that was actually supplied.
Perfect score. The check can only fail if the model writes `[S17]` when it was given 16
sources. It says nothing about whether the cited passage is the right one.

The one wrong answer shows the gap. q188 asks how Capital One's liquidity-risk disclosure
changed between its 2024 and 2026 10-Ks. The 2024 10-K reports an average LCR of 167% for
the fourth quarter of 2023 and liquidity reserves of $120.7 billion. The answer's "2024"
column holds 155% and $123.8 billion, which are the fourth-quarter 2024 figures from the
2025 10-K. Its conclusion, "LCR improved significantly (155% → 173%)", compares the wrong
years.

Every number in that answer is real, and every citation points at the passage that
contains it. Citation validity scores it 1.0, and a claim-by-claim faithfulness check would
pass it too, because each claim is supported by what it cites. What is wrong is which
filing the passage came from, and none of the standard metrics look at that.

## The negative it answered

The system declined 18 of the 19 questions with no answer. The one it didn't was q070:
*"What interest rate decision did the European Central Bank make at its most recent
meeting?"* The corpus has no ECB documents, so the right response is to decline.

It failed in two ways. First, FOMC minutes summarise what foreign central banks did in the
staff review, so the retriever returned passages that mention the ECB and the model
answered from them. Second, the newest minutes it was given, from June 2026, report an ECB
rate increase. The model called that source "future-dated" and reported a December 2024
cut as the most recent decision. Its own sense of the current date overrode the dates on
the documents in front of it.

It is wrong under either reading of the question. If the ECB is out of scope, it should
have declined. If second-hand reporting in the minutes counts, the answer is the June 2026
increase.

q188 and q070 have the same shape as the seven partial answers: something close to the
question was retrieved and answered as if it were the question. That is what this system
is bad at.

## Why unanswerable questions belong in the golden set

The standard advice is to build an evaluation set of questions with known answers, and
score retrieval and generation against them. That set cannot measure any of the above,
because every question in it *has* an answer. A system that answers everything scores the
same as one that knows when to stop.

Nineteen of my 202 questions have no answer in the corpus. They come in two kinds, and the
distinction turned out to matter:

- **Unanswerable, in-domain** (11). Plausible questions about facts the companies do not
  disclose. *"What is Capital One's customer acquisition cost per new credit card
  account?"* Retrieval will happily return pages of adjacent prose about marketing spend.
- **Out-of-scope** (8). Questions about entities the corpus does not contain.
  *"What guidance did Tesla give for vehicle deliveries?"*

The out-of-scope ones are easy and I nearly cut them. They earn their place by failing
differently. The system declines them because retrieval returns nothing plausible, which
is a different mechanism from declining because it recognised that plausible passages did
not contain the fact. Keeping both is how you tell "the router worked" apart from "the
model exercised judgement". q070 is the case where it did neither.

181 of the 183 answerable questions were drafted by Claude Opus from sampled passages and
checked by script: each gold passage has to be verbatim from its filing and findable in the
index. The other two are q055 and q063.
The drafter and the system under test are both Claude models, which probably flatters the
retriever, since the questions are phrased the way that family phrases things.

The negatives had to be written by hand, because a model asked to invent a
plausible-but-absent fact will reliably invent one that is actually present somewhere in
24,650 filing chunks. Two of my first attempts turned out to be answerable. Two of the 21
that made it into the set did too, and I only found them by searching the corpus for each
one. Writing a negative from an assumption about where a fact is disclosed isn't enough.

## The tradeoff nobody reports

Once negatives exist, you get two numbers instead of one, and they move against each other:

| | |
|---|---|
| **abstention recall** | of questions with no answer, the share it declined | **0.9474** |
| **over-refusal rate** | of answerable questions, the share it wrongly declined | **0.1202** |

Either alone is trivially gameable. Refuse everything and abstention recall is 1.000.
Answer everything and over-refusal is 0.000. Reporting one without the other is a choice
about which way you would like to look good.

The pair also makes the operating point visible as a choice. Mine sits at 0.95 / 0.12: the
system declines 18 of 19 questions it should, and wrongly declines about 1 in 8 it could
have answered. For a finance research assistant I would take that trade again, since a
wrong number costs more than a missing one, but it is a trade, and it should be argued
rather than defaulted into.

With 19 negatives, one question changing its mind moves abstention recall by more than five
points. The 95% interval on 18 of 19 runs from about 75% to 99%.

## The taxonomy, and what it exposed

Scoring is not diagnosis. I classify every failure by its **earliest cause**, so a question
that both misroutes and then synthesises badly is charged to routing:

```
183 answerable questions
  correct                          137  (75%)
  retrieval miss (answered anyway)  18  (10%)
  over-refusal                       9   (5%)
  retrieval miss (abstained)         7   (4%)
  routing error                      6   (3%)
  wrong synthesis                    6   (3%)
```

This table is the scorer's view. By reading, 8 of the 18 "answered anyway" were right, as
above.

The two `retrieval miss` rows are the same underlying event, retrieval not finding the
evidence, separated only by what the system did next. They do not add up to the 13/18
split above, and the gap is the point of charging to earliest cause. Thirteen questions
abstained after a retrieval miss. Six of those were also misrouted, so the taxonomy charges
them to routing, where the fix actually lives. The earlier count is of the retrieval event;
the table is of what to go and fix.

## What this cost, and what I would keep

Four things earned their time:

**Confidence intervals on everything.** My golden set cannot resolve effects smaller than
about 0.12. I computed that before running the ablation, and it retired several
"improvements" that were noise with an ordering printed on them. Paired bootstrap over
per-question scores, not two independent means.

**A ceiling check.** Before trusting a recall number I verified that every gold span is
reachable at all: retrievability 1.0000. Without it, a low score is ambiguous between bad
retrieval and a broken metric.

**Reporting macro and micro.** They differ by 0.04 here (0.7377 vs 0.7000), which is larger
than most effects I measured. Quoting only the flattering one would have been a choice.

**Reading the failures.** The first version of this post had its arithmetic right and
its main example wrong, because I never opened the passage the system cited. Metrics tell you where to look. They don't tell you what you'll find.

And one thing I would tell anyone starting: the hard part is not building the evaluation,
it is keeping it pointed at the thing you actually mean. I found four cases where a check
had drifted: a quality gate scoring a config the service never ran, a fixture missing
evidence for the questions it graded, a threshold calibrated at a context size that had
since doubled, and a failing test pointed at an answer that was correct. Each produced
plausible numbers and no error. Each was caught only by comparing against the real thing
rather than against something that resembled it.

---

*Code, golden set, and the full failure log:
[github.com/omvyas77/finhelm](https://github.com/omvyas77/finhelm). Live demo:
[huggingface.co/spaces/omvyas77/finhelm](https://huggingface.co/spaces/omvyas77/finhelm).
The metrics here come from one frozen run, `semantic-hybrid-rr-ctx-ag-final`, re-scored
after the two label fixes. `tests/test_readme_traces_to_a_run.py` recomputes them, and the
counts above, from that run.*
