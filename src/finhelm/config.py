"""Every tunable knob, in one frozen dataclass.

Each eval run logs `asdict(cfg)` to MLflow, so an ablation row can be traced back to the
exact configuration that produced it.
"""

from dataclasses import dataclass

# Vector width per embedding model, as data rather than inferred from the model: a store
# needs the number to create its table before anything has been embedded.
EMBED_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "intfloat/e5-base-v2": 768,
    "intfloat/e5-large-v2": 1024,
}


@dataclass(frozen=True)
class Config:
    # chunking
    chunking: str = "fixed"  # fixed | semantic | sentence_window
    chunk_tokens: int = 800
    chunk_overlap: int = 120
    sentence_window: int = 3

    # embeddings + store
    embed_model: str = "BAAI/bge-small-en-v1.5"
    store: str = "faiss"  # faiss | pgvector
    # Prepend issuer/form/period/section to a chunk before embedding it. Requires an
    # index built the same way, so the flag also selects the index directory
    # (see stores.index_name).
    contextual_headers: bool = False
    # BGE/E5 are trained asymmetrically: passages embedded bare, queries carrying a
    # retrieval instruction. On by default; the flag exists so the effect can be measured.
    query_prefix: bool = True

    # retrieval
    retriever: str = "dense"  # dense | bm25 | hybrid
    rrf_k: int = 60
    # How pools from *different sub-questions* are combined. "rrf" sums reciprocal ranks,
    # which demotes anything specific to one half of a comparison; "interleave"
    # round-robins and preserves within-pool rank (retrieve/hybrid.py::interleave). Dense
    # and BM25 fusion *within* a collection is separate and always RRF.
    cross_query_fusion: str = "rrf"  # rrf | interleave
    # Restrict a single-issuer sub-question to that issuer's filings before ranking.
    # Issuer only, never date: see agent/decompose.py::filters_for.
    # On by default, worth +0.0470 [+0.0193, +0.0773] paired against the same config
    # without it. filters_for() returns None when a question names zero or several
    # issuers, so a corpus without tickers never filters. run_name marks the control arm
    # "-noflt" rather than marking the treatment.
    filter_by_issuer: bool = True
    # Rerank each sub-question's pool against that sub-question and take a quota from
    # each, instead of scoring one merged pool against the compound original.
    # See retrieve/rerank.py::rerank_per_query.
    rerank_per_subquestion: bool = False
    rerank: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"
    # Score every window of an over-long passage and keep its best, instead of only the
    # prefix that fits the cross-encoder's 512-token budget. On by default: +0.0635
    # [+0.0221, +0.1050] paired over 181 questions, the largest effect measured here.
    # 44% of query+passage pairs exceed the window; 24% of pooled multi-span gold spans
    # sat past the cut. Costs about 1.5x the rerank pairs.
    rerank_windows: bool = True
    top_k_retrieve: int = 20
    top_k_context: int = 8

    # agentic
    # Whether the router may fall back to an LLM call when its keyword heuristic finds no
    # signal. On in production; off in CI, where the gate must be free and deterministic.
    # Safe to disable for a recall gate: with no signal the router searches both
    # collections, a superset of what the model would pick, so recall is only understated.
    # Not safe for route_accuracy, which collapses, since half the golden set reaches the
    # LLM path.
    llm_router: bool = True
    agentic: bool = False
    # Decompose to extract *filters* only, then retrieve with the single original query.
    # Turning `agentic` off removes decomposition, cross-query RRF fusion and
    # per-sub-question filtering together, and the last of those is a known positive
    # (filter coverage: 75% on a compound question, 99% on sub-questions). This arm keeps
    # the filters and drops the split queries, which separates the two contributions.
    agentic_filters_only: bool = False
    max_sub_questions: int = 4
    agent_timeout_s: int = 30

    # generation
    gen_model: str = "claude-sonnet-4-6"
    # Must be a different family from gen_model; __post_init__ enforces it.
    # Three candidates were rejected, none of them obviously:
    #   2.5-flash      still listed by models.list(), but 404s for keys issued after its
    #                  retirement, so being listed is not evidence it is callable.
    #   3.6-flash      ignores temperature, so a judge cannot be pinned to 0 and identical
    #                  inputs re-score differently between runs.
    #   3.5-flash      honours temperature but runs out of throughput partway through a
    #                  full pass, and the RESOURCE_EXHAUSTED was retried until a timeout
    #                  fired, so it surfaced as TimeoutError and then NaN metrics.
    judge_model: str = "gemini-3.1-flash-lite"
    max_tokens: int = 1024
    temperature: float = 0.0

    @property
    def embed_dim(self) -> int:
        """Vector width, derived from the model rather than stored beside it.

        This was a plain field defaulting to 384: right for bge-small, wrong for the
        bge-base now shipped, and read by nothing, so the disagreement went unnoticed
        until pgvector needed the width in DDL and `vector(384)` rejected every 768-dim
        insert. Unknown models raise rather than guess, because a wrong width fails at
        insert or, if the numbers line up, returns nonsense at query time.
        """
        try:
            return EMBED_DIMS[self.embed_model]
        except KeyError:
            raise ValueError(
                f"unknown embedding width for {self.embed_model!r}; add it to EMBED_DIMS"
            ) from None

    def run_name(self) -> str:
        """Deterministic MLflow run name derived from the config."""
        return (
            f"{self.chunking}-{self.retriever}"
            f"{'-rr' if self.rerank else ''}"
            f"{'-ctx' if self.contextual_headers else ''}"
            f"{'' if self.query_prefix else '-noprefix'}"
            f"{'-il' if self.cross_query_fusion == 'interleave' else ''}"
            f"{'' if self.filter_by_issuer else '-noflt'}"
            f"{'' if self.chunk_tokens == 800 else f'-t{self.chunk_tokens}'}"
            f"{'' if self.rrf_k == 60 else f'-rrf{self.rrf_k}'}"
            f"{'' if self.rerank_windows else '-nowin'}"
            f"{'' if self.rerank_model.endswith('reranker-base') else '-' + self.rerank_model.split('-')[-1]}"
            f"{'-rpq' if self.rerank_per_subquestion else ''}"
            f"{'-ag' if self.agentic else ''}"
            f"{'' if self.top_k_retrieve == 20 else f'-k{self.top_k_retrieve}'}"
        )

    def __post_init__(self) -> None:
        if self.judge_model and self.judge_model.startswith("claude") == self.gen_model.startswith("claude"):
            raise ValueError(
                "gen_model and judge_model must be different model families "
                "(same-family judging produces self-preference bias)"
            )


# The configuration this project serves.
#
# Defined here, not in api.py: the Streamlit demo ships without FastAPI and died on
# `ModuleNotFoundError: No module named 'fastapi'` when reading this constant. api.CONFIG
# re-exports it, so callers and the tests pinned to it are unaffected.
SERVED = Config(
    chunking="semantic", retriever="hybrid", rerank=True, agentic=True,
    contextual_headers=True, embed_model="BAAI/bge-base-en-v1.5",
    top_k_context=16,
)
