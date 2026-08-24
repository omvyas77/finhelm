"""Single source of truth for every tunable knob.

Every eval run logs `asdict(cfg)` to MLflow, which is what makes the ablation
table reproducible without hand-tracking what changed between runs.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # chunking
    chunking: str = "fixed"  # fixed | semantic | sentence_window
    chunk_tokens: int = 800
    chunk_overlap: int = 120
    sentence_window: int = 3

    # embeddings + store
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    store: str = "faiss"  # faiss | pgvector
    # Prepend issuer/form/period/section to a chunk before embedding it. Off reproduces
    # the Day 2 indexes exactly; on requires an index built with it, which is why the
    # flag also selects the index directory (see stores.index_name).
    contextual_headers: bool = False
    # BGE/E5 are trained asymmetrically: passages are embedded bare, queries carry a
    # retrieval instruction. On by default because omitting it is simply wrong for these
    # models; the flag exists so the effect can be measured rather than assumed, which is
    # what the 74-span golden set was unable to do.
    query_prefix: bool = True

    # retrieval
    retriever: str = "dense"  # dense | bm25 | hybrid
    rrf_k: int = 60
    rerank: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"
    top_k_retrieve: int = 20
    top_k_context: int = 8

    # agentic
    agentic: bool = False
    max_sub_questions: int = 4
    agent_timeout_s: int = 30

    # generation
    gen_model: str = "claude-sonnet-4-6"
    # Must be a DIFFERENT family than gen_model.
    #
    # Not 2.5-flash: that checkpoint still appears in models.list() but returns 404 "no
    # longer available to new users" for keys issued after its retirement, so being
    # listed is not evidence it is callable.
    #
    # Not 3.6-flash either, despite being the newer model the 404 recommends: it uses
    # fixed sampling defaults and silently ignores temperature. A judge that cannot be
    # pinned to temperature=0 re-scores identical inputs differently between runs, which
    # would show up in the ablation table as movement that no code change caused.
    #
    # Not 3.5-flash either. It honours temperature, but a full Ragas pass is ~4 metrics
    # over ~54 answered questions with per-context sub-calls underneath, and this key's
    # throughput for it runs out partway through. The interesting part is how that
    # failed: RESOURCE_EXHAUSTED got retried by tenacity until the per-job timeout fired,
    # so quota exhaustion presented as TimeoutError and then as NaN metrics. Nothing in
    # the output said "rate limited".
    judge_model: str = "gemini-3.1-flash-lite"
    max_tokens: int = 1024
    temperature: float = 0.0

    def run_name(self) -> str:
        """Deterministic MLflow run name derived from the config."""
        return (
            f"{self.chunking}-{self.retriever}"
            f"{'-rr' if self.rerank else ''}"
            f"{'-ctx' if self.contextual_headers else ''}"
            f"{'' if self.query_prefix else '-noprefix'}"
            f"{'-ag' if self.agentic else ''}"
            f"{'' if self.top_k_retrieve == 20 else f'-k{self.top_k_retrieve}'}"
        )

    def __post_init__(self) -> None:
        if self.judge_model and self.judge_model.startswith("claude") == self.gen_model.startswith("claude"):
            raise ValueError(
                "gen_model and judge_model must be different model families "
                "(same-family judging produces self-preference bias)"
            )
