---
title: finhelm
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: streamlit
sdk_version: 1.62.0
app_file: app.py
pinned: false
short_description: Question answering over US bank SEC filings that shows its work
---

# finhelm

Ask a question about a US bank's SEC filings, Federal Reserve statements, or CFPB consumer
complaints. Every answer is built only from passages retrieved out of those documents, and
every passage is shown underneath with a link to the original filing.

Measured on 202 human-verified questions: **recall@16 0.7403**, **citation validity
1.0000**, abstention recall 0.9048. Source and the full evaluation harness:
[github.com/omvyas77/finhelm](https://github.com/omvyas77/finhelm)

**First question after idle takes 60-90 seconds** — the Space sleeps, and the embedding
model and cross-encoder load on the first request. Later questions settle around 30s, most
of which is retrieval and generation rather than boot.

**It declines more than you might expect, on purpose.** Two of the four example questions
are ones it should refuse: a plausible-sounding figure companies do not disclose, and a
company outside the corpus. A finance assistant that invents a number is worse than one
that says no.
