"""Ask Finhelm a question from the terminal.

    python scripts/ask.py "What did JPM say about CRE exposure in 2025?"
    python scripts/ask.py --retriever hybrid --show-sources "..."
    python scripts/ask.py --retrieve-only "..."     # no LLM call, just what came back

--retrieve-only exists because most bad answers are bad retrievals, and paying for a
generation to discover that wastes both time and money.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finhelm.config import Config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--chunking", default="fixed",
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--retriever", default="dense", choices=["dense", "bm25", "hybrid"])
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--ticker", default=None, help="metadata filter, e.g. JPM")
    ap.add_argument("--collection", default=None, choices=["filings", "complaints"],
                    help="skip the router and force a collection")
    ap.add_argument("--retrieve-only", action="store_true")
    ap.add_argument("--show-sources", action="store_true")
    args = ap.parse_args()

    overrides = {"chunking": args.chunking, "retriever": args.retriever}
    if args.top_k:
        overrides["top_k_context"] = args.top_k
    cfg = dataclasses.replace(Config(), **overrides)

    filters = {"ticker": args.ticker} if args.ticker else None
    collections = [args.collection] if args.collection else None

    if args.retrieve_only:
        from finhelm.retrieve import retrieve

        found = retrieve(args.question, cfg, filters, collections)
        print(f"route: {'+'.join(found.route.collections)} "
              f"({found.route.method}: {found.route.reason})\n")
        for i, hit in enumerate(found.hits, 1):
            m = hit.metadata
            print(f"[S{i}] {hit.score:.4f} {m.get('ticker') or m.get('source')} "
                  f"{m.get('form')} {m.get('date')} · {m.get('section')}")
            print(f"     {hit.text[:200].strip()}...\n")
        return

    from finhelm.generate import answer

    result = answer(args.question, cfg, filters, collections)

    print(f"route: {result.route} ({result.route_reason})")
    print(f"config: {result.config} · trace {result.trace_id}")
    print(f"latency: retrieval {result.retrieval_ms}ms · generation {result.generation_ms}ms")
    print("-" * 78)
    print(result.answer)
    print("-" * 78)
    print(f"cited: {result.cited_ids or 'none'} of {len(result.retrieved)} sources")
    if result.invalid_citations:
        print(f"INVALID CITATIONS: {result.invalid_citations}")
    if result.uncited_sentences:
        print(f"uncited sentences: {result.uncited_sentences}")
    if result.abstained:
        print("abstained: yes")

    if args.show_sources:
        print()
        for i, hit in enumerate(result.retrieved, 1):
            m = hit.metadata
            print(f"[S{i}] {m.get('ticker') or m.get('source')} {m.get('form')} "
                  f"{m.get('date')} · {m.get('section')} · {m.get('url', '')}")


if __name__ == "__main__":
    main()
