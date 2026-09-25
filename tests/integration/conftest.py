"""Live checks are explicitly selected; an ordinary pytest run stays offline."""
import json
import os
from pathlib import Path

import pytest

from integration.azure_checks import Acceptance
from scripts.api import acquire_token


def pytest_addoption(parser):
    group = parser.getgroup("azure")
    for name in ("azure-resources", "azure-kubeconfig", "azure-study", "azure-section"):
        group.addoption("--" + name)
    group.addoption("--azure-evidence", default="data/evidence/azure")


@pytest.fixture(scope="session")
def azure(request):
    names = ("azure_resources", "azure_kubeconfig", "azure_study", "azure_section")
    values = [request.config.getoption(name) for name in names]
    if not all(values):
        pytest.skip("Live Azure target/study not supplied; offline run")
    resources_file, kubeconfig, study, section = values
    resources = json.loads(Path(resources_file).read_text())
    evidence = Path(request.config.getoption("azure_evidence"))
    operator_token = os.getenv("MEDW_API_TOKEN")
    token_provider = None if operator_token else lambda: acquire_token(
        resources, Path("data/api/token-cache.json"))
    collector = Acceptance(resources, kubeconfig, evidence, study, section,
                           token=operator_token, token_provider=token_provider)
    collector.report["scope"] = "selected_integration_tests"
    yield collector
    collector.report["passed"] = bool(collector.report["checks"]) and all(
        check["status"] == "passed" for check in collector.report["checks"].values())
    collector.save()


@pytest.fixture(scope="session")
def application(azure):
    result = azure.check("application_workflow", azure.application)
    assert azure.report["checks"]["application_workflow"]["status"] == "passed"
    return result


@pytest.fixture(scope="session")
def nightly_batch(azure, application):
    result = azure.check("airflow_batch", azure.airflow_batch)
    assert azure.report["checks"]["airflow_batch"]["status"] == "passed"
    return result
