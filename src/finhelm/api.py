"""HTTP surface for the retrieval pipeline.

Four endpoints, and one of them is unusual on purpose: `/eval-report` serves the most
recent evaluation result, so the running service exposes its own measured quality rather
than asking anyone to trust a number in a README. A system that reports recall@16 of 0.7403
and an over-refusal rate of 0.1160 to whoever asks is making a checkable claim.

`/config` exists for the same reason. Every retrieval knob that has been argued about in
this project — chunk size, pool width, the RRF constant, whether windowing is on — is a
Config field, so returning `asdict(cfg)` makes the running configuration inspectable
instead of inferred from the deployment.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Config
from .generate import answer
from .telemetry import log_request, setup, span

ROOT = Path(__file__).resolve().parents[2]
HISTORY = ROOT / "evals" / "history.jsonl"

# Configured before the app is instrumented. Returns False and costs nothing when no
# collector is set, which is the case for the eval harness and for running the service
# standalone.
# No name passed: OTEL_SERVICE_NAME decides, so the same image registers as
# finhelm-api or finhelm-ui depending on which service compose started it as.
TRACING = setup()

app = FastAPI(
    title="finhelm",
    description="Retrieval-augmented question answering over SEC filings, FOMC "
                "statements and CFPB complaints.",
    version="0.3.0",
)

# The configuration that produced the numbers /eval-report serves — not the best
# configuration measured anywhere.
#
# rrf_k stays at the default 60 despite the fusion sweep preferring 20 at this pool width
# (pool recall 0.8197 against 0.8026). That sweep was a simulation over cached retrieval
# and was never run end-to-end, so shipping it would mean serving an unmeasured pipeline
# alongside a measured quality claim. The gap is worth about +0.017 of pool recall and can
# be adopted the moment a real run confirms it.
CONFIG = Config(
    chunking="semantic", retriever="hybrid", rerank=True, agentic=True,
    contextual_headers=True, embed_model="BAAI/bge-base-en-v1.5",
    top_k_context=16,
)


if TRACING:
    # Auto-instrumentation nests HTTP spans above the ask/retrieve/rerank/generate spans
    # the pipeline emits, so a trace shows the request boundary and the stage breakdown in
    # one waterfall rather than two disconnected trees.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)
    # Both optional overrides exist for debugging a bad answer against the live index —
    # narrowing the collections or forcing a ticker filter is how you tell "retrieval
    # never saw it" apart from "the model ignored it".
    collections: list[str] | None = Field(
        None, description="restrict to 'filings' and/or 'complaints'")
    filters: dict | None = Field(
        None, description="metadata filter, e.g. {'ticker': 'JPM'}")
    # The one knob a demo has any business flipping. Left unset the service uses its
    # pinned config; the Streamlit UI offers it as a toggle, and without it here that
    # toggle silently did nothing whenever the UI was talking to the API — which is
    # every deployment, since compose always wires FINHELM_API_URL.
    agentic: bool | None = Field(
        None, description="override query decomposition for this request")


class Citation(BaseModel):
    marker: str
    chunk_id: str
    doc_id: str | None
    ticker: str | None
    date: str | None
    section: str | None
    url: str | None
    score: float


class AskResponse(BaseModel):
    answer: str
    abstained: bool
    route: str
    route_reason: str
    sub_questions: list[str]
    citations: list[Citation]
    # Reported rather than hidden: an invalid marker means the model cited a source that
    # was never supplied, which is the failure a reader cannot see for themselves.
    invalid_citations: list[str]
    uncited_sentences: int
    retrieval_ms: int
    generation_ms: int
    trace_id: str
    config: str


@app.middleware("http")
async def request_id(request: Request, call_next):
    """Adopt the caller's request id, or mint one.

    Honouring an inbound X-Request-ID is what lets a trace span a gateway, this service and
    whatever called it; generating one when absent means every request is traceable
    regardless of what the client does.
    """
    incoming = request.headers.get("x-request-id")
    request.state.request_id = incoming or uuid.uuid4().hex[:16]
    started = time.monotonic()
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    response.headers["x-response-time-ms"] = f"{(time.monotonic() - started) * 1000:.0f}"
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Return the request id with the error.

    A 500 with no correlation id is unactionable — the caller cannot tell you which
    request failed, and the logs cannot be joined to it.
    """
    return JSONResponse(
        status_code=500,
        content={"error": type(exc).__name__, "detail": str(exc),
                 "request_id": getattr(request.state, "request_id", None)},
    )


@app.get("/health")
def health() -> dict:
    """Liveness plus whether the artifacts the service needs are actually present.

    Reporting "ok" while the FAISS index is missing would make the container pass its
    healthcheck and fail every request, which is worse than failing to start.
    """
    from .stores import INDEX_DIR, index_name

    index = INDEX_DIR / index_name("filings", CONFIG.chunking,
                                   CONFIG.contextual_headers, CONFIG.embed_model,
                                   CONFIG.chunk_tokens)
    ready = index.exists()
    return {"status": "ok" if ready else "degraded",
            "index": index.name, "index_present": ready,
            "version": app.version}


@app.get("/config")
def config() -> dict:
    return {"config": asdict(CONFIG), "run_name": CONFIG.run_name()}


@app.get("/eval-report")
def eval_report() -> dict:
    """The most recent evaluation this configuration produced.

    Keyed on the live config's run name where possible, so the service reports the quality
    of what it is actually running rather than the best number on disk.
    """
    if not HISTORY.exists():
        return {"detail": "no evaluation history"}
    rows = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    if not rows:
        return {"detail": "no evaluation history"}
    name = CONFIG.run_name()
    matching = [r for r in rows if r.get("run_name", "").startswith(name)]
    row = (matching or rows)[-1]
    return {
        "run_name": row.get("run_name"),
        "matched_live_config": bool(matching),
        "n_questions": row.get("n_questions"),
        "metrics": {k: v for k, v in row.items()
                    if isinstance(v, (int, float)) and k != "n_questions"},
    }


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, http_request: Request) -> AskResponse:
    request_id = getattr(http_request.state, "request_id", None)
    # replace() on a frozen dataclass, so the pinned CONFIG is never mutated by a request.
    config = (CONFIG if request.agentic is None
              else replace(CONFIG, agentic=request.agentic))
    with span("ask", **{"request.id": request_id,
                        "query.chars": len(request.question),
                        "query.agentic": config.agentic}):
        result = answer(request.question, config, request.filters, request.collections)

    citations = []
    for i, hit in enumerate(result.retrieved, start=1):
        marker = f"S{i}"
        if marker not in result.cited_ids:
            continue
        meta = hit.metadata
        citations.append(Citation(
            marker=marker, chunk_id=hit.chunk_id, doc_id=meta.get("doc_id"),
            ticker=meta.get("ticker"), date=str(meta.get("date") or "") or None,
            section=meta.get("section"), url=meta.get("url"),
            score=round(float(hit.score), 4),
        ))

    # One line per request, emitted whether or not a collector is listening. Chunk ids
    # rather than chunk text — enough to reconstruct what retrieval returned for a request
    # that went wrong, without putting filing prose in the log stream.
    log_request(
        request_id=request_id, trace_id=result.trace_id, config=result.config,
        route=result.route, route_reason=result.route_reason,
        sub_questions=len(result.sub_questions),
        retrieved=[h.chunk_id for h in result.retrieved],
        abstained=result.abstained, cited=len(result.cited_ids),
        invalid_citations=result.invalid_citations or None,
        uncited_sentences=result.uncited_sentences,
        retrieval_ms=result.retrieval_ms, generation_ms=result.generation_ms,
    )

    return AskResponse(
        answer=result.answer, abstained=result.abstained, route=result.route,
        route_reason=result.route_reason, sub_questions=result.sub_questions,
        citations=citations, invalid_citations=result.invalid_citations,
        uncited_sentences=result.uncited_sentences,
        retrieval_ms=result.retrieval_ms, generation_ms=result.generation_ms,
        # The generator's own trace id, not the HTTP one: it is what appears on the
        # OpenTelemetry spans and in the structured log line for this request.
        trace_id=result.trace_id, config=result.config,
    )
