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
    gen_model: str = "claude-opus-4-6"
    judge_model: str = "gemini-2.5-flash"  # must be a DIFFERENT family than gen_model
    max_tokens: int = 1024
    temperature: float = 0.0

    def run_name(self) -> str:
        """Deterministic MLflow run name derived from the config."""
        return (
            f"{self.chunking}-{self.retriever}"
            f"{'-rr' if self.rerank else ''}"
            f"{'-ag' if self.agentic else ''}"
        )

    def __post_init__(self) -> None:
        if self.judge_model and self.judge_model.startswith("claude") == self.gen_model.startswith("claude"):
            raise ValueError(
                "gen_model and judge_model must be different model families "
                "(same-family judging produces self-preference bias)"
            )
