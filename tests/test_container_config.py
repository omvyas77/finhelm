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
