---
title: finhelm
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Q&A over US bank filings that shows its work
---

# finhelm

Ask a question about a US bank's SEC filings, Federal Reserve statements, or CFPB consumer
complaints. Every answer is built only from passages retrieved out of those documents, and
every passage is shown underneath with a link to the original filing.

Measured on 202 questions, 181 of them drafted by Claude Opus and checked by script and 21
written by hand to have no answer: **recall@16 0.7377**. It declines 18 of the 19 questions
with no answer in the corpus, and wrongly declines 22 of the 183 that have one. Source and
the full evaluation harness:
[github.com/omvyas77/finhelm](https://github.com/omvyas77/finhelm)

**The first question after idle is slow.** The Space sleeps, and the embedding model and
cross-encoder load on the first request. The evaluation run's median was 32 seconds a
question once warm, most of it spent finding and ranking passages.

**It declines more than you might expect, on purpose.** Two of the four example questions
are ones it should refuse: a plausible-sounding figure companies do not disclose, and a
company outside the corpus. A finance assistant that invents a number is worse than one
that says no.

**Each session gets 10 questions**, because every answer is paid for. To keep going, run it
yourself from the repository.
