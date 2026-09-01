"""Streamlit demo.

Written for someone who has never seen the project. The point of the interface is not the
answer — it is showing the evidence the answer was built from, because a RAG system that
hands you a paragraph and hides its sources is asking to be trusted rather than checked.

Talks to the FastAPI service when FINHELM_API_URL is set (how compose wires it) and calls
the library directly otherwise, so `streamlit run app.py` works on its own.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
# src/, not the repo root. Adding the root made `src.finhelm` importable as a namespace
# package, and importing the same code under two names gives you two module objects: two
# CONFIGs, two sets of lru_caches, and — in a container where PYTHONPATH already points at
# /app/src — a second copy of the models resident in memory.
sys.path.insert(0, str(ROOT / "src"))

API_URL = os.getenv("FINHELM_API_URL")
HISTORY = ROOT / "evals" / "history.jsonl"

st.set_page_config(page_title="finhelm", page_icon="📊", layout="wide")

# Each example demonstrates a different behaviour, including two different reasons for
# declining. Refusal is the least discoverable thing this system does and the most
# important: a finance assistant that invents a number is worse than one that says no.
EXAMPLES = [
    ("Answers from a filing",
     "What factors does Discover Financial Services identify as increasing its "
     "cybersecurity risk exposure in its 2024 10-K?",
     "A single fact from a single document. Watch it cite the exact passage."),
    ("Compares two banks",
     "How do Capital One and Bank of America each address the FDIC special assessment "
     "related to the Silicon Valley Bank failure?",
     "Needs evidence from two different filings. It splits the question in two, "
     "retrieves for each half, then answers."),
    ("Declines — not disclosed",
     "What is Capital One's customer acquisition cost per new credit card account?",
     "A reasonable-sounding question about a number companies do not publish. "
     "It should refuse rather than estimate."),
    ("Declines — outside the corpus",
     "What guidance did Tesla give for vehicle deliveries in 2026?",
     "Tesla is not in this corpus at all. Different reason for refusing, same honesty."),
]


@st.cache_data(ttl=300)
def latest_eval() -> dict | None:
    if not HISTORY.exists():
        return None
    rows = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    scored = [r for r in rows if r.get("recall_at_16") and (r.get("n_questions") or 0) > 100]
    return scored[-1] if scored else None


def ask(question: str, agentic: bool) -> dict:
    if API_URL:
        import requests
        response = requests.post(f"{API_URL}/ask",
                                 json={"question": question, "agentic": agentic},
                                 timeout=180)
        response.raise_for_status()
        return response.json()

    import dataclasses

    from finhelm.api import CONFIG
    from finhelm.generate import answer

    result = answer(question, dataclasses.replace(CONFIG, agentic=agentic))
    return {
        "answer": result.answer, "abstained": result.abstained, "route": result.route,
        "route_reason": result.route_reason, "sub_questions": result.sub_questions,
        "retrieval_ms": result.retrieval_ms, "generation_ms": result.generation_ms,
        "invalid_citations": result.invalid_citations,
        "citations": [
            {"marker": f"S{i}", "chunk_id": h.chunk_id,
             "doc_id": h.metadata.get("doc_id"), "ticker": h.metadata.get("ticker"),
             "date": str(h.metadata.get("date") or ""), "section": h.metadata.get("section"),
             "url": h.metadata.get("url"), "score": round(float(h.score), 4),
             "text": h.text}
            for i, h in enumerate(result.retrieved, start=1)
        ],
    }


# ----------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("How this works")
    st.markdown(
        "Ask a question about a US bank's SEC filings, Federal Reserve statements, or "
        "consumer complaints. The system:\n\n"
        "1. **Decides where to look** — company filings or consumer complaints\n"
        "2. **Splits the question** if it needs facts from more than one document\n"
        "3. **Searches twice** — by meaning and by keyword — and merges the results\n"
        "4. **Re-reads the best candidates** closely and keeps the top 16\n"
        "5. **Answers using only those passages**, citing each claim\n\n"
        "If the passages do not contain the answer, it says so instead of guessing."
    )

    st.divider()
    st.subheader("Measured quality")
    row = latest_eval()
    if row:
        st.caption(f"From `{row['run_name']}` over {row.get('n_questions', 0):.0f} questions.")
        left, right = st.columns(2)
        left.metric("Finds the evidence", f"{row.get('recall_at_16', 0):.0%}",
                    help="Share of the passages needed to answer that reach the model.")
        right.metric("Citations valid", f"{row.get('citation_validity', 0):.0%}",
                     help="Every source marker points at a real supplied source. "
                          "Nothing invented.")
        left.metric("Refuses when it should", f"{row.get('abstention_recall', 0):.0%}",
                    help="Of questions with no answer in the corpus, how many it declined.")
        right.metric("Cost per question", f"${row.get('cost_usd_per_query', 0):.3f}")
        st.caption(
            "Finding the evidence is the hard part and the honest number. When retrieval "
            "works the answer is almost always right; when it fails the system usually "
            "declines rather than inventing."
        )
    else:
        st.info("No evaluation on disk yet.")

    st.divider()
    st.subheader("Settings")
    agentic = st.toggle(
        "Split complex questions", value=True,
        help="Break a comparison into one question per company before searching. "
             "Slower, and how the two-bank example works.")
    st.caption(f"Backend: {'API at ' + API_URL if API_URL else 'in-process'}")


# -------------------------------------------------------------------------- main
st.title("finhelm")
st.markdown(
    "**A question-answering system over US bank filings that shows its work.** "
    "Every answer is built only from passages retrieved out of real SEC filings, Federal "
    "Reserve statements and CFPB consumer complaints — and every one of those passages is "
    "shown to you underneath, with a link to the original document."
)

st.subheader("Try one of these")
st.caption("Each shows the system doing something different.")
columns = st.columns(len(EXAMPLES))
for column, (label, question, explanation) in zip(columns, EXAMPLES):
    with column:
        if st.button(label, use_container_width=True):
            st.session_state.question = question
        st.caption(explanation)

question = st.text_area(
    "Or ask your own",
    value=st.session_state.get("question", ""),
    height=90,
    placeholder="e.g. How did JPMorgan describe its commercial real estate exposure "
                "in its 2024 10-K?",
)
submitted = st.button("Ask", type="primary")

if submitted and question.strip():
    with st.spinner("Searching filings, then reading the best passages..."):
        try:
            result = ask(question.strip(), agentic)
        except Exception as exc:
            st.error(f"Request failed: {type(exc).__name__}: {exc}")
            st.stop()

    if result["abstained"]:
        st.warning("**The system declined to answer.**")
        st.markdown(result["answer"])
        st.caption(
            "This is the intended behaviour when the retrieved passages do not contain "
            "the answer — the alternative is a confident, invented figure."
        )
    else:
        st.success("**Answer**")
        st.markdown(result["answer"])
        st.caption(
            "Markers like [S1] refer to the numbered sources below. Every factual "
            "sentence should carry one."
        )

    if result.get("invalid_citations"):
        st.error(
            f"Cited sources that were never supplied: {result['invalid_citations']}. "
            "This is a hallucinated citation and is counted as a failure."
        )

    a, b, c, d = st.columns(4)
    a.metric("Searched", result["route"].replace("+", " + "))
    b.metric("Sub-questions", len(result.get("sub_questions") or []) or "—")
    c.metric("Retrieval", f"{result['retrieval_ms'] / 1000:.1f}s")
    d.metric("Answering", f"{result['generation_ms'] / 1000:.1f}s")
    if result.get("sub_questions"):
        with st.expander("How the question was split"):
            for sub in result["sub_questions"]:
                st.markdown(f"- {sub}")

    citations = result.get("citations") or []
    with st.expander(f"The {len(citations)} passages it read", expanded=not result["abstained"]):
        st.caption(
            "Ranked by how well each passage matches the question. Debugging this system "
            "means reading these — a wrong answer is nearly always a wrong passage."
        )
        for citation in citations:
            header = " · ".join(x for x in [citation.get("ticker"), citation.get("date"),
                                            (citation.get("section") or "").replace("_", " ")]
                                if x)
            st.markdown(f"**[{citation['marker']}]** {header} — score `{citation['score']}`")
            if citation.get("url"):
                st.markdown(f"[Open the original filing]({citation['url']})")
            st.text(citation.get("text", "")[:900] or "")
            st.divider()

st.caption(
    "Known limitation: on questions about facts companies do not disclose, the system "
    "occasionally answers anyway from a plausible-looking passage. That failure is tracked "
    "in the evaluation suite rather than hidden — see the abstention figure in the sidebar."
)
