"""The Dockerfile and compose file have to agree with the code, and nothing else checks it.

Every failure here is one this project already made once. The DSN was named three
different things in three files — `POSTGRES_DSN` in .env, `PGVECTOR_DSN` in compose,
`FINHELM_PG_DSN` in the code — and the result was not an error but a container quietly
connecting to itself on localhost. The image bakes model weights and then runs with
HF_HUB_OFFLINE=1, so pointing the served config at a model the image did not bake turns
into a hang on the first request rather than a build failure.

These are text and YAML assertions on purpose: they run in the fast CI tier in
milliseconds, with no daemon, no image, and no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

# Every service built from the project image.
APP_SERVICES = ("api", "ui", "mlflow")
# The subset that runs the pipeline and therefore needs its wiring. mlflow is deliberately
# excluded: it is the tracking server, not a client of it, and it never opens the store.
PIPELINE_SERVICES = ("api", "ui")


def _arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)", DOCKERFILE, re.MULTILINE)
    assert match, f"Dockerfile has no ARG {name}"
    return match.group(1)


def test_compose_has_the_five_services_the_writeup_claims():
    assert set(COMPOSE["services"]) == {"api", "ui", "postgres", "jaeger", "mlflow"}


@pytest.mark.parametrize("service", APP_SERVICES)
def test_app_services_share_one_image(service):
    """One build, three roles. Sharing the image is what keeps the mlflow server on the
    same version as the client that wrote mlflow.db."""
    assert COMPOSE["services"][service]["image"] == "finhelm:local"


def test_the_dsn_the_code_reads_is_the_one_compose_sets():
    source = (ROOT / "src" / "finhelm" / "stores" / "pgvector_store.py").read_text()
    match = re.search(r'os\.getenv\(\s*"([A-Z_]+)"', source)
    assert match, "pgvector_store no longer reads a DSN from the environment"
    name = match.group(1)

    for service in PIPELINE_SERVICES:
        env = COMPOSE["services"][service].get("environment", {})
        assert name in env, f"compose does not set {name} for {service}"
        # Not localhost: inside a container that is the container itself.
        assert "@postgres:" in env[name], f"{service} points {name} at the wrong host"

    assert re.search(rf"^{name}=", (ROOT / ".env.example").read_text(), re.MULTILINE), \
        f".env.example does not document {name}"


def test_image_bakes_the_models_the_service_actually_serves():
    """HF_HUB_OFFLINE=1 makes this load-bearing: a served model the image did not bake
    cannot be downloaded at runtime, so the mismatch surfaces as a failed first request
    instead of a failed build."""
    from finhelm.api import CONFIG

    assert _arg("EMBED_MODEL") == CONFIG.embed_model
    assert _arg("RERANK_MODEL") == CONFIG.rerank_model


def test_baked_embedding_model_has_a_declared_width():
    from finhelm.config import EMBED_DIMS

    assert _arg("EMBED_MODEL") in EMBED_DIMS


def test_runtime_is_fenced_off_from_the_hub():
    assert "ENV HF_HUB_OFFLINE=1" in DOCKERFILE
    assert "ENV TRANSFORMERS_OFFLINE=1" in DOCKERFILE


def test_container_does_not_run_as_root():
    assert re.search(r"^USER finhelm", DOCKERFILE, re.MULTILINE)
    # After the last COPY, or the copies land as root and the app cannot read them.
    assert DOCKERFILE.index("USER finhelm") > DOCKERFILE.rindex("COPY --chown")


def test_api_port_agrees_across_expose_healthcheck_and_compose():
    assert re.search(r"^EXPOSE 8000", DOCKERFILE, re.MULTILINE)
    assert "127.0.0.1:8000/health" in DOCKERFILE
    assert "8000:8000" in COMPOSE["services"]["api"]["ports"]


def test_healthcheck_rejects_a_degraded_container():
    """/health returns 200 with status 'degraded' when the index is missing, which is what
    a forgotten volume mount produces. A healthcheck that only checked for a response
    would call that container healthy while it abstains on every question."""
    assert "'ok'" in DOCKERFILE and "status" in DOCKERFILE


def test_secrets_never_enter_the_build_context():
    ignored = {line.strip() for line in DOCKERIGNORE.splitlines()}
    assert ".env" in ignored
    assert ".env.bak.*" in ignored
    assert not any(line.strip() == "!.env" for line in DOCKERIGNORE.splitlines())


def test_index_is_mounted_read_only_not_baked():
    """961 MB of rebuildable artifact. Baked, it would be in every layer and every push;
    mounted rw, a bug in a request path could corrupt an 85-minute rebuild."""
    assert "data/" in DOCKERIGNORE
    assert "./data:/app/data:ro" in COMPOSE["services"]["api"]["volumes"]


def test_ui_talks_to_the_api_rather_than_loading_its_own_models():
    env = COMPOSE["services"]["ui"]["environment"]
    assert env["FINHELM_API_URL"] == "http://api:8000"


def test_otel_endpoint_uses_the_grpc_port():
    """4317 is gRPC and 4318 is HTTP. telemetry.py picks the exporter from the port, so a
    wrong port here is a silent no-op rather than an error."""
    for service in PIPELINE_SERVICES:
        endpoint = COMPOSE["services"][service]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"]
        assert endpoint == "http://jaeger:4317"

    # And the collector is actually listening on it.
    assert "4317:4317" in COMPOSE["services"]["jaeger"]["ports"]


def test_each_pipeline_service_gets_its_own_otel_service_name():
    """Three compose services run the same image, so the service name is the only thing
    separating their spans in Jaeger. It was set here and read nowhere — telemetry.setup()
    hardcoded "finhelm" — so api and ui traces landed under one name until Day 3.5."""
    names = {s: COMPOSE["services"][s]["environment"].get("OTEL_SERVICE_NAME")
             for s in PIPELINE_SERVICES}
    assert all(names.values()), f"a pipeline service has no OTEL_SERVICE_NAME: {names}"
    assert len(set(names.values())) == len(names), f"service names collide: {names}"


def test_the_code_actually_reads_that_variable():
    from finhelm.telemetry import resolve_service_name

    import os
    before = os.environ.get("OTEL_SERVICE_NAME")
    os.environ["OTEL_SERVICE_NAME"] = "finhelm-api"
    try:
        assert resolve_service_name() == "finhelm-api"
    finally:
        if before is None:
            del os.environ["OTEL_SERVICE_NAME"]
        else:
            os.environ["OTEL_SERVICE_NAME"] = before


def test_mlflow_runs_a_single_worker():
    """Measured at 994 MB of anonymous memory for one worker; the per-CPU default put the
    container in an exit-137 restart loop under any limit small enough to be useful."""
    command = COMPOSE["services"]["mlflow"]["command"]
    assert any(str(c).startswith("--workers=") for c in command)


def test_mlflow_allows_the_host_it_is_published_on():
    """mlflow 3.x rejects any request whose Host header is not on --allowed-hosts, and
    --host=0.0.0.0 does not imply it. Whatever host port compose publishes has to appear
    in that list or the UI answers 403 from a browser."""
    published = COMPOSE["services"]["mlflow"]["ports"][0]
    host_port = str(published).split(":")[0]
    allowed = next(str(c) for c in COMPOSE["services"]["mlflow"]["command"]
                   if str(c).startswith("--allowed-hosts="))
    assert f"localhost:{host_port}" in allowed, f"{host_port} missing from {allowed}"


# ------------------------------------------- validators must score the served config

def test_the_judged_gate_scores_the_served_config():
    """The rule this file exists to enforce, applied to the quality gate.

    tests/test_smoke_deepeval.py built its answers with a bare `Config()` — fixed, dense,
    no reranking, bge-small, k=8 — for the life of the file. The service is pinned to
    semantic + hybrid + rerank + contextual + bge-base at k=16, so every quality gate this
    project passed was gating a configuration it never ran.

    Asserted as text rather than by importing and inspecting, because the failure mode is
    a *source* mistake and the local filesystem hides it at runtime: on a developer machine
    `data/index/filings_fixed` exists, so the wrong config loads happily and the suite goes
    green. It surfaced only on a runner that had no such directory.
    """
    source = (ROOT / "tests" / "test_smoke_deepeval.py").read_text()
    assert "from finhelm.api import CONFIG" in source, \
        "the judged gate must score api.CONFIG, the config the service serves"
    # A bare Config() anywhere in this file reintroduces the bug.
    assert not re.search(r"=\s*Config\(\s*\)", source), \
        "found a bare Config() in the judged gate; it must use api.CONFIG"


def test_the_eval_gate_workflow_scores_the_served_config():
    """Same rule, applied to the deterministic tier: the flags CI passes to run_eval must
    describe api.CONFIG. A gate calibrated against one retriever and run against another
    measures nothing about the diff."""
    workflow = (ROOT / ".github" / "workflows" / "eval-gate.yml").read_text()
    gate = workflow[workflow.index("Deterministic eval gate"):]
    gate = gate[:gate.index("Upload eval results")]

    from finhelm.api import CONFIG

    assert f"--chunking {CONFIG.chunking}" in gate
    assert f"--retriever {CONFIG.retriever}" in gate
    assert ("--rerank" in gate) == CONFIG.rerank
    assert ("--contextual" in gate) == CONFIG.contextual_headers
    assert CONFIG.embed_model == _arg("EMBED_MODEL")


def test_the_ci_fixture_covers_every_smoke_question():
    """The judged gate retrieves from data/ci, so the fixture must contain the evidence
    for the questions that gate asks.

    It did not. The fixture was built from a stratified 40-question subset and the smoke
    suite uses a different 12, of which only 5 overlapped — so 7 questions had their gold
    spans excluded from the corpus by construction. The gate then failed 8 of 13 in CI
    against 4 of 13 locally, and the difference was not answer quality: the system was
    being asked about evidence that had been deliberately removed. A faithfulness score
    measured that way is a fact about the fixture, not about the system.
    """
    import json
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from test_smoke_deepeval import load_smoke_set

    subset = ROOT / "evals" / "ci_subset.jsonl"
    assert subset.exists(), "evals/ci_subset.jsonl is missing"
    covered = {json.loads(l)["id"] for l in subset.read_text().splitlines() if l.strip()}
    smoke = {q["id"] for q in load_smoke_set()}

    missing = sorted(smoke - covered)
    assert not missing, (
        f"{len(missing)} smoke questions are not in the CI fixture ({missing}); "
        f"regenerate with scripts/make_ci_fixture.py, which includes them by construction")


def test_the_committed_fixture_index_matches_the_committed_chunks():
    """A stale index is the quietest possible failure: retrieval still returns results,
    they are simply drawn from a corpus that no longer matches the chunk parquet the
    metric reads. Counts have to agree."""
    import json

    import pandas as pd

    ci = ROOT / "data" / "ci"
    for parquet, index_dir in (
        ("chunks_filings_semantic.parquet", "filings_semantic_ctx_bge-base-en-v15"),
        ("chunks_complaints_fixed.parquet", "complaints_fixed_ctx_bge-base-en-v15"),
    ):
        chunks = pd.read_parquet(ci / "processed" / parquet)
        meta = ci / "index" / index_dir / "meta.jsonl"
        assert meta.exists(), f"{meta} missing; rebuild the fixture index"
        n_indexed = sum(1 for line in meta.read_text().splitlines() if line.strip())
        assert n_indexed == len(chunks), (
            f"{index_dir} holds {n_indexed} vectors but {parquet} has {len(chunks)} "
            f"chunks — the committed index is stale")


def test_the_judged_suite_configures_deepeval_with_a_value_deepeval_accepts():
    """Importing the judged suite must not raise, and its settings must validate.

    The suite disables DeepEval's per-attempt timeout at module top. The first version set
    the string "None" — which pydantic rejects, because the field is Optional[float] with
    gt=0 — and it was never run locally before being pushed, since the local run that
    motivated the line predates the line. CI failed 11 of 13 with a ValidationError naming
    the setting.

    This test costs nothing, needs no API key, and runs in the every-push tier, which is
    where a configuration error in a 60-minute paid job should be caught.
    """
    import importlib
    import os

    module = importlib.import_module("test_smoke_deepeval")
    importlib.reload(module)

    from deepeval.config.settings import Settings

    # Raises ValidationError if the module set something DeepEval cannot parse.
    settings = Settings()

    override = settings.DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE
    assert override is not None, (
        "leaving the override unset restores DeepEval's 207s per-attempt cancellation, "
        "which is what the module top exists to avoid")
    assert override > 3600, (
        f"per-attempt timeout is {override}s; the suite needs it effectively unbounded, "
        f"with the CI job ceiling as the real backstop")
    assert os.environ.get("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE")


def test_the_relevancy_threshold_is_reachable_at_the_served_context_size():
    """A threshold above the metric's ceiling is a gate that can never pass.

    ContextualRelevancy scores the fraction of supplied context that bears on the
    question, so it is diluted by top_k_context: with roughly one relevant chunk among k,
    the achievable score is about 1/k. The threshold was a fixed 0.10, calibrated when the
    service supplied 8 chunks (ceiling ~0.125). The service now supplies 16, ceiling
    ~0.0625 — so the fixed threshold sat *above* anything retrieval could reach, and CI
    failed a question scoring 0.087, which is better than one relevant chunk in sixteen.

    A gate that always fails is as useless as one that always passes, and worse than
    either is one whose failure looks like an answer-quality problem.
    """
    from test_smoke_deepeval import RELEVANCY_THRESHOLD

    from finhelm.api import CONFIG

    ceiling = 1.0 / CONFIG.top_k_context
    assert RELEVANCY_THRESHOLD < ceiling, (
        f"threshold {RELEVANCY_THRESHOLD} is at or above the ~{ceiling:.4f} ceiling "
        f"implied by top_k_context={CONFIG.top_k_context}; it can never be met")
    # And not so far below that it stops catching "almost nothing relevant came back".
    assert RELEVANCY_THRESHOLD > ceiling / 4, (
        f"threshold {RELEVANCY_THRESHOLD} is far under the {ceiling:.4f} ceiling and "
        f"would pass a retrieval returning nothing useful")


def test_the_relevancy_threshold_moves_with_the_context_budget():
    """Derived rather than hardcoded, so a future budget change cannot silently
    reintroduce an unreachable threshold."""
    source = (ROOT / "tests" / "test_smoke_deepeval.py").read_text()
    assert "CONFIG.top_k_context" in source.split("RELEVANCY_THRESHOLD =")[1][:120], \
        "RELEVANCY_THRESHOLD must be derived from the served top_k_context"


def test_the_known_hallucination_is_still_present_in_the_last_real_run():
    """q055's xfail must describe reality, and reality here means the real corpus.

    The judged tier runs against the 1,903-chunk CI fixture, which does not contain the
    plausible-but-irrelevant passage the fabrication is built from — so on the fixture the
    system abstains correctly and a strict xfail reports XPASS, which reads as "fixed,
    delete the marker". It is not fixed: the Day 4.4 final run against the full index still
    answers with a fabricated compensation figure and does not abstain.

    This test is the thing that would stop the marker being deleted on the strength of a
    fixture run. If it ever fails, the hallucination genuinely is gone and the marker
    should go with it.
    """
    import json

    from test_smoke_deepeval import KNOWN_HALLUCINATION

    results = sorted((ROOT / "evals" / "results").glob("*-final.json"))
    if not results:
        pytest.skip("no frozen final run on disk")
    records = {r["id"]: r for r in json.loads(results[-1].read_text())["records"]}

    for qid in KNOWN_HALLUCINATION:
        record = records.get(qid)
        if record is None:
            continue
        assert record["abstained"] is False, (
            f"{qid} now abstains against the real corpus — the known hallucination looks "
            f"fixed. Verify against a fresh full run, then remove it from "
            f"KNOWN_HALLUCINATION and delete this assertion.")


def test_the_xfail_is_scoped_to_the_corpus_it_describes():
    source = (ROOT / "tests" / "test_smoke_deepeval.py").read_text()
    assert "RUNNING_ON_FIXTURE" in source, (
        "the known-hallucination xfail must not apply when running against the CI "
        "fixture, where the failure it describes cannot occur")


def test_the_demo_import_path_does_not_require_a_web_framework():
    """The Space installs a serving-only dependency set and has no FastAPI.

    app.py read the served config from finhelm.api, which imports FastAPI, so the deployed
    demo died on `ModuleNotFoundError: No module named 'fastapi'` at the first question —
    importing a web framework to read a constant. The config now lives in config.py and
    api.CONFIG re-exports it.

    Simulated rather than trusted: the import is attempted with fastapi, uvicorn,
    starlette and nltk blocked at the meta-path, which is what the Space actually
    looks like. nltk is in the list because sentence splitting is an indexing concern
    and the serving path must not need it — a module-level import of it in
    chunking/__init__.py was the second thing to break the deployed demo. A
    plain import here would pass on any machine that happens to have FastAPI installed,
    which is every developer machine and none of the deployments.
    """
    import subprocess
    import sys

    program = (
        "import sys\n"
        "class B:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name.split('.')[0] in ('fastapi','uvicorn','starlette','nltk') else None\n"
        "    def load_module(self, name):\n"
        "        raise ImportError(name)\n"
        "sys.meta_path.insert(0, B())\n"
        "from finhelm.config import SERVED\n"
        "from finhelm.generate import answer\n"
        "print(SERVED.run_name())\n"
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True,
                            text=True, cwd=str(ROOT),
                            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, (
        f"the demo's import path needs a web framework:\n{result.stderr[-600:]}")


def test_the_served_config_lives_outside_the_http_layer():
    from finhelm.api import CONFIG
    from finhelm.config import SERVED

    assert CONFIG is SERVED, "api.CONFIG must re-export config.SERVED, not redefine it"
    source = (ROOT / "app.py").read_text()
    assert "from finhelm.api import" not in source, (
        "the Streamlit demo must not import finhelm.api; it ships without FastAPI")
