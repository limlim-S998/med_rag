# Provenance: one object, every exit point.
#
# The version axes were previously spelled out separately in the audit INSERT,
# in the metric dimensions, and in whatever log line needed them. Three copies
# of one fact means a fourth axis gets added in two places and forgotten in the
# third — and the forgetting is silent, because a missing column in an audit
# row looks exactly like a row written before that axis existed.

import dataclasses

import pytest

from medw_core.provenance import Provenance
from medw_core.settings import Settings

# Every field that, if it changed, would change the output. Kept as a literal
# list rather than derived from the dataclass, so ADDING an axis fails this
# test and forces the decision about whether it belongs on metrics too.
VERSION_AXES = {
    "image_sha",
    "chat_deployment",
    "embed_version",
    "prompt_bundle_sha",
    "classifier_version",
}


@pytest.fixture
def prov() -> Provenance:
    return Provenance.from_settings(
        Settings(service_name="generation", image_sha="4f2a91c", env="prod")
    )


def test_audit_row_carries_every_version_axis(prov):
    """The audit row is the record of what produced a paragraph. An axis
    missing from it cannot be reconstructed later from anything else."""
    assert prov.as_dict().keys() >= VERSION_AXES


def test_audit_row_locates_the_process(prov):
    """env and service are not version axes, but a row that cannot be traced
    to a cluster and a service is not much of an audit trail."""
    row = prov.as_dict()
    assert row["env"] == "prod"
    assert row["service"] == "generation"


def test_image_sha_is_not_a_metric_dimension(prov):
    """Every distinct dimension combination is a separate time series, and
    image_sha changes on every deploy — so putting it on a metric creates a new
    series per release, forever. It belongs on the audit row, where it is one
    column on one row."""
    assert "image_sha" in prov.as_dict()
    assert "image_sha" not in prov.as_metric_dimensions()


def test_metric_dimensions_are_a_subset_of_the_audit_row(prov):
    """Anything dimensioned on a metric must also be recoverable from the
    audit trail, or you can see that something changed without being able to
    find out what it changed to."""
    assert prov.as_metric_dimensions().keys() <= prov.as_dict().keys()


def test_provenance_is_frozen(prov):
    """Captured at startup. If it could be mutated it would stop answering
    "what produced this" and start answering "what is configured now"."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        prov.image_sha = "tampered"  # type: ignore[misc]


def test_reads_effective_settings_not_defaults():
    """The value that matters is what this process actually loaded. During a
    rollout some pods are on the old prompt bundle and some are not, and a row
    reporting intended configuration is confidently wrong exactly then."""
    p = Provenance.from_settings(Settings(prompt_bundle_sha="abc1234"))
    assert p.prompt_bundle_sha == "abc1234"


def test_unset_image_sha_is_visibly_unknown():
    """Outside Kubernetes nothing sets MEDW_IMAGE_SHA. It must read as
    "unknown" rather than an empty string, so an audit row from a laptop is
    distinguishable from one where the injection silently failed."""
    assert Provenance.from_settings(Settings()).image_sha == "unknown"


def test_helm_injects_the_image_sha():
    """The Settings default is "unknown"; the chart has to override it or every
    pod reports unknown and the code axis is lost in exactly the environment
    that needs it."""
    import pathlib

    tpl = (pathlib.Path(__file__).resolve().parent.parent
           / "deploy/charts/medw-lib/templates/_deployment.yaml").read_text()
    assert "MEDW_IMAGE_SHA" in tpl, "the deployment template must inject the image SHA"
    assert ".Values.image.tag" in tpl, "it must come from the image tag, not a separate value"
