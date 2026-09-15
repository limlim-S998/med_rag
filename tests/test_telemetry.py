"""Startup exporters and HTTP provenance must agree on actual packaged content."""

from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from medw_core import metrics, tracing
from medw_core.content import prompt_hash
from medw_core.service import add_platform_routes, attach_request_instrumentation, lifespan_for
from medw_core.settings import Settings


def test_lifespan_exports_the_same_effective_identity_as_version(monkeypatch, tmp_path):
    configure_metrics, configure_traces, configure_logs = Mock(), Mock(), Mock()
    monkeypatch.setattr(metrics, "configure", configure_metrics)
    monkeypatch.setattr(tracing, "configure", configure_traces)
    monkeypatch.setattr(tracing, "configure_logging", configure_logs)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "draft.md").write_text("Synthetic test prompt")
    settings = Settings(_env_file=None, backend="local", env="test",
                        local_state_path=str(tmp_path / "state.db"),
                        local_artifact_dir=str(tmp_path / "artifacts"),
                        prompt_bundle_sha="stale-intent", appinsights_connection_string="",
                        image_sha="test-source", image_digest="sha256:test-image")
    app = FastAPI(lifespan=lifespan_for("generation", settings, prompts=prompts))
    attach_request_instrumentation(app, "generation")
    add_platform_routes(app, settings)
    # Attaching middleware does not eagerly configure a provider without its settings.
    configure_metrics.assert_not_called()
    configure_traces.assert_not_called()
    with TestClient(app) as client:
        version = client.get("/version").json()
        assert version["service"] == "generation"
        assert version["prompt_bundle_sha"] == prompt_hash(prompts)
        assert version["prompt_bundle_sha"] != settings.prompt_bundle_sha
        resource = configure_metrics.call_args.kwargs["resource_attributes"]
        assert resource["medw.prompt_bundle_sha"] == version["prompt_bundle_sha"]
        assert resource["service.version"] == version["image_sha"]
        assert resource["medw.image_digest"] == version["image_digest"]
        configure_metrics.assert_called_once_with("", "generation", resource_attributes=resource)
        configure_traces.assert_called_once_with("generation", "", resource_attributes=resource)
        configure_logs.assert_called_once_with(settings.log_level, "generation")
