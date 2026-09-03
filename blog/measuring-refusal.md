# The number that matters is what your RAG system does when retrieval fails

Most RAG write-ups report a retrieval score and stop. Mine is 0.7403 — recall@16 over 202
questions, macro-averaged, 95% interval [0.682, 0.793]. It is the least interesting number
in the project.

Here is the interesting one. Of the 181 answerable questions in my golden set, there are
**30 where retrieval did not surface the evidence at all**. The system abstained on 12 of
them. It answered the other **18 anyway**.

So when this system fails to find the evidence, it produces a confident, cited,
well-formatted answer **60% of the time**.

Two things keep that honest. It is conditional: retrieval fails on 30 of 181 answerable
questions, so the unconditional rate of answering with nothing behind it is **10%**, not
60%. And it sits beside a number that looks much better — on the 21 questions with no
answer anywhere in the corpus, the system refuses **19 of them, 90%**.

Put together those two figures say something sharper than either alone. The system is good
at recognising *nothing is here* and weak at recognising *something is here, but not the
thing you asked for*. The second case is harder, because the passages come back looking
plausible, and it is the one that matters — a question with no answer is rare in
production, and a question whose answer your retriever missed is not.

That number does not appear in any standard RAG metric. You cannot get to it from recall,
or faithfulness, or answer relevancy. You get to it by deliberately building a golden set
that can expose it, and most golden sets cannot.

---

## Why unanswerable questions belong in the golden set

The standard advice is to build an evaluation set of questions with known answers, and
score retrieval and generation against them. That set is structurally incapable of
measuring the failure above, because every question in it *has* an answer. A system that
answers everything scores identically to one that knows when to stop.

Twenty-one of my 202 questions have no answer in the corpus. They come in two kinds, and
the distinction turned out to matter:

- **Unanswerable, in-domain** (13). Plausible questions about facts the companies do not
  disclose. *"What is Capital One's customer acquisition cost per new credit card
  account?"* Retrieval will happily return pages of adjacent prose about marketing spend.
- **Out-of-scope** (8). Questions about entities the corpus does not contain.
  *"What guidance did Tesla give for vehicle deliveries?"*

The out-of-scope ones are easy and I nearly cut them. They earn their place by failing
differently: the system declines them because retrieval returns nothing plausible, which is
a different mechanism from declining because it recognised that plausible passages did not
contain the fact. Keeping both is how you tell "the router worked" apart from "the model
exercised judgement".

Writing these by hand is non-negotiable and slow. I drafted the answerable questions with a
model and verified every one; the negatives had to be written by hand, because a model asked
to invent a plausible-but-absent fact will reliably invent one that is *actually present*
somewhere in a 24,650-chunk corpus. Two of my first attempts turned out to be answerable.

## The tradeoff nobody reports

Once negatives exist, you get two numbers instead of one, and they move against each other:

| | |
|---|---|
| **abstention recall** | of questions with no answer, the share it declined | **0.9048** |
| **over-refusal rate** | of answerable questions, the share it wrongly declined | **0.1160** |

Either alone is trivially gameable. Refuse everything and abstention recall is 1.000.
Answer everything and over-refusal is 0.000. Reporting one without the other is not a
measurement, it is a choice about which way you would like to look good.

The pair also makes an operating point *visible as a choice*. Mine sits at 0.90 / 0.12:
the system declines 19 of 21 questions it should, and wrongly declines about 1 in 9 it
could have answered. For a finance research assistant I would take that trade again — a
wrong number costs more than a missing one — but it is a trade, and it should be argued
rather than defaulted into.

## The taxonomy, and what it exposed

Scoring is not diagnosis. I classify every failure by its **earliest cause**, so a question
that both misroutes and then synthesises badly is charged to routing:

```
181 answerable questions
  correct                          136  (75%)
  retrieval miss (answered anyway)  18  (10%)   <- the dangerous one
  over-refusal                       9   (5%)
  routing error                      6   (3%)
  retrieval miss (abstained)         6   (3%)   <- the good failure
  wrong synthesis                    6   (3%)
```

The two `retrieval miss` rows are the same underlying event — retrieval did not find the
evidence — separated only by what the system did next.

They also do not add up to the 12/18 split I opened with, and the gap is the point of
charging to earliest cause. Twelve questions abstained after a retrieval miss; six of those
were *also* misrouted, so the taxonomy charges them to routing, where the fix actually
lives. Fixing routing would recover those six regardless of what retrieval did afterwards.
The opening counts the retrieval event; the table counts what to go and fix. Both are
correct and they answer different questions, which is worth stating rather than letting a
reader find the arithmetic and assume one of them is wrong. One is a system that knows its limits. The other is the
failure the whole project exists to prevent. A single "accuracy" number averages them
together and hides the distinction entirely.

## Citation validity is 1.0000, and it is not what it sounds like

Every citation marker in every answer points at a source that was actually supplied.
Perfect score. It is also nearly meaningless as a safety property, and one question in my
set proves it.

q055 asks for Jamie Dimon's 2025 compensation, which is not in the corpus. The system
answers:

> $43,000,000 total compensation for fiscal year 2025, consisting of an annual base salary
> of $1,500,000 and performance-based variable incentive compensation of $41,500,000...

Itemised, plausible, and cited to a real JPMorgan 8-K cover page. The figure is invented.
**Citation validity scores this answer 1.0**, because the marker points at a real supplied
source — it just does not contain the claim.

That question is a permanent, strict `xfail` in the test suite. It nearly got deleted:
when the judged CI tier ran against a smaller fixture corpus, the passage that enables the
fabrication was not retrieved, the system abstained correctly, and the strict xfail flipped
to XPASS — which reads exactly like *fixed, remove this marker*. It was not fixed. The
failure is corpus-dependent, and deleting the marker on that evidence would have retired
the only tracked instance of the exact failure the system exists to prevent, for the most
persuasive reason available: a green test telling you to.

## What this cost, and what I would keep

Three things earned their time:

**Confidence intervals on everything.** My golden set cannot resolve effects smaller than
about 0.12 — I computed that before running the ablation, and it retired several
"improvements" that were noise with an ordering printed on them. Paired bootstrap over
per-question scores, not two independent means.

**A ceiling check.** Before trusting a recall number I verified that every gold span is
reachable at all: retrievability 1.0000. Without it, a low score is ambiguous between bad
retrieval and a broken metric.

**Reporting macro and micro.** They differ by 0.04 here (0.7403 vs 0.7016), which is larger
than most effects I measured. Quoting only the flattering one would have been a choice.

And one thing I would tell anyone starting: **the hard part is not building the evaluation,
it is keeping it pointed at the system you actually ship.** I found four separate cases
where a validator had drifted from the served configuration — a quality gate scoring a
config the service never ran, a fixture missing evidence for the questions it graded, a
threshold calibrated at a context size that had since doubled, and the xfail above. Each
produced plausible numbers and no error. Each was caught only by comparing against the real
thing rather than against something that resembled it.

---

*Code, golden set, and the full failure log:
[github.com/omvyas77/finhelm](https://github.com/omvyas77/finhelm). Live demo:
[huggingface.co/spaces/omvyas77/finhelm](https://huggingface.co/spaces/omvyas77/finhelm).
Every number here comes from one frozen run, `semantic-hybrid-rr-ctx-ag-final`, and a test
asserts the README cannot quote a figure that run did not produce.*
