"""Evidence from real application traffic and explicitly selected recovery."""
import pytest

pytestmark = pytest.mark.azure


def check(azure, name, action):
    result = azure.check(name, action)
    assert azure.report["checks"][name]["status"] == "passed", azure.report["checks"][name]
    return result


def test_readiness_and_public_routing(azure):
    check(azure, "release_readiness", azure.ready_releases)
    check(azure, "public_access", azure.public_access)


def test_access_controls(azure):
    check(azure, "access_and_checksum_rejections", azure.negative_access)


def test_data_flow(azure, application):
    check(azure, "blob_source_checksum", azure.blob_content)
    check(azure, "sql_audit", lambda: azure.audit_rows([application["draft"]["draft_id"]]))


def test_nightly_batch(nightly_batch):
    assert nightly_batch


@pytest.mark.recovery
def test_worker_restart_and_publication(azure, application):
    check(azure, "worker_restart_recovery", azure.worker_recovery)


@pytest.mark.recovery
def test_qdrant_persistence_and_backup(azure, application):
    check(azure, "qdrant_persistence", azure.persistence)
    check(azure, "blob_snapshot_restore", azure.backup_restore)


@pytest.mark.recovery
def test_airflow_metadata_backup(azure, nightly_batch):
    check(azure, "airflow_metadata_restore", azure.airflow_backup_restore)


@pytest.mark.load
def test_observability_and_scaling(azure, application):
    check(azure, "metrics_and_scaling", azure.monitoring_and_scaling)
    check(azure, "correlated_traces", azure.traces)
