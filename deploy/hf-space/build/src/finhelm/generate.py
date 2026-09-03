"""Answer generation with machine-checkable citations.

Two design choices carry the entire an earlier stage evaluation:

Numbered sources ([S1]...[Sn]) make citation validity *countable*. A model that cites [S9]
when eight sources were supplied has hallucinated in a way that is detectable without a
judge model, without embeddings, and without human review — a regex finds it.

The exact-string abstention rule (INSUFFICIENT_CONTEXT:) makes refusal countable for the
same reason. "I'm not sure, but probably..." is unparseable; a literal sentinel is not.
That is what turns abstention recall and over-refusal into two separate numbers rather
than one vague impression, and those two numbers trade against each other — which is the
finding the eval exists to measure.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

from .config import Config
from .llm import claude
from .retrieve import retrieve
from .telemetry import set_attributes, span
from .stores.base import Hit

SYSTEM = """You are a financial research assistant. Answer ONLY from the numbered
sources below.

Rules:
1. Every factual claim ends with a citation marker: [S1], [S3], or [S1][S4].
2. Cite only source numbers that appear below. Never invent a source number.
3. If the sources do not contain enough information, reply with exactly:
   INSUFFICIENT_CONTEXT: <one sentence on what is missing>
   Do not guess, and do not answer from general knowledge.
4. If sources disagree or come from different periods, say so and cite both.
5. Be concise. No preamble."""

ABSTAIN_PREFIX = "INSUFFICIENT_CONTEXT:"
_CITATION = re.compile(r"\[S(\d+)\]")


def build_context(hits: list[Hit]) -> str:
    blocks = []
    for i, hit in enumerate(hits, start=1):
        m = hit.metadata
        # ticker is None for FOMC and CFPB rows, so fall back to the source name. An
        # unlabelled block is one the model cannot attribute to a company or a period.
        who = m.get("ticker") or m.get("source", "?")
        blocks.append(
            f"[S{i}] ({who} {m.get('form', '?')} {m.get('date', '?')}, "
            f"{m.get('section', '?')})\n{hit.text}"
        )
    return "\n\n".join(blocks)


@dataclass
class Answer:
    answer: str
    cited_ids: list[str]
    retrieved: list[Hit]
    route: str
    abstained: bool
    invalid_citations: list[str]
    uncited_sentences: int
    retrieval_ms: int
    generation_ms: int
    trace_id: str
    config: str
    route_reason: str = ""
    sub_questions: list[str] = field(default_factory=list)


def _audit(text: str, n_sources: int) -> tuple[list[str], list[str], int]:
    """Markers used, markers pointing at sources that were never supplied, and the number
    of substantive sentences carrying no citation at all."""
    used, invalid = [], []
    for num in _CITATION.findall(text):
        marker = f"S{num}"
        if marker not in used:
            used.append(marker)
        if not 1 <= int(num) <= n_sources and marker not in invalid:
            invalid.append(marker)

    uncited = sum(
        1
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if len(sentence.split()) >= 5 and not _CITATION.search(sentence)
    )
    return used, invalid, uncited


def answer(
    question: str,
    cfg: Config | None = None,
    filters: dict | None = None,
    collections: list[str] | None = None,
) -> Answer:
    cfg = cfg or Config()
    # Minted here rather than at the API boundary: threading a trace id back through
    # retrieval and generation after the fact is miserable, and an earlier stage needs it on spans.
    trace_id = uuid.uuid4().hex[:16]

    started = time.monotonic()
    with span("retrieve"):
        found = retrieve(question, cfg, filters, collections)
    retrieval_ms = int((time.monotonic() - started) * 1000)
    set_attributes(**{"query.route": "+".join(found.route.collections),
                      "query.agentic": cfg.agentic,
                      "query.sub_questions": len(found.sub_questions),
                      "retrieve.hits": len(found.hits),
                      "retrieve.ms": retrieval_ms})

    if not found.hits:
        # Abstaining without calling the model keeps "retrieval found nothing" and "the
        # model declined to answer" distinguishable in the an earlier stage numbers. Collapsing them
        # would hide a retrieval outage as an abstention win.
        return Answer(
            answer=f"{ABSTAIN_PREFIX} retrieval returned no matching sources.",
            cited_ids=[], retrieved=[], route="+".join(found.route.collections),
            abstained=True, invalid_citations=[], uncited_sentences=0,
            retrieval_ms=retrieval_ms, generation_ms=0, trace_id=trace_id,
            config=cfg.run_name(), route_reason=found.route.reason,
        )

    prompt = f"{build_context(found.hits)}\n\nQuestion: {question}"
    started = time.monotonic()
    with span("generate", **{"gen.model": cfg.gen_model,
                             "gen.sources": len(found.hits),
                             "gen.prompt_chars": len(prompt)}) as generation:
        text = claude(
            prompt, cfg.gen_model, system=SYSTEM,
            max_tokens=cfg.max_tokens, temperature=cfg.temperature,
        ).strip()
        if generation is not None:
            generation.set_attribute("gen.abstained", text.startswith(ABSTAIN_PREFIX))
            generation.set_attribute("gen.answer_chars", len(text))
    generation_ms = int((time.monotonic() - started) * 1000)

    used, invalid, uncited = _audit(text, len(found.hits))
    return Answer(
        answer=text,
        cited_ids=used,
        retrieved=found.hits,
        route="+".join(found.route.collections),
        abstained=text.startswith(ABSTAIN_PREFIX),
        invalid_citations=invalid,
        uncited_sentences=uncited,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        trace_id=trace_id,
        config=cfg.run_name(),
        route_reason=found.route.reason,
        # Carried through from retrieval. The field existed on Answer from the start and
        # was never populated, so every generating run recorded an empty list and
        # decomposition was untraceable in exactly the runs that cost money to produce.
        sub_questions=list(found.sub_questions),
    )
