# One object that says what produced a given output.
#
# The repo claims orthogonal version axes and then spelled them out three
# separate times: `audit.py` read them off Settings to build an INSERT,
# `metrics.py` would have needed them as metric dimensions, and every log line
# that mattered would have needed them again. Three copies of one fact is how
# a fourth axis gets added in two places and forgotten in the third.
#
# The rule this enforces: a version identifier is captured from the *effective*
# configuration of the running process, never from intent. A row that reports
# what the values file was supposed to say - rather than what this pod actually
# loaded - is worse than no row, because it is confidently wrong during exactly
# the situation you need it for: a half-completed rollout where some pods are
# on the old prompt bundle and some are not.

from __future__ import annotations

from dataclasses import asdict, dataclass

from medw_core.settings import Settings


@dataclass(frozen=True)
class Provenance:
    """The version identity of one running process.

    Frozen and built once at startup. If it could change at runtime it would
    stop being an answer to "what produced this" and become an answer to "what
    was configured when someone last looked".

    Source SHA, image digest, release/config identity, prompt content and
    expected model identity are distinct. A deployment-name suffix alone
    does not establish its model version. Environment and service identify
    where the process runs, not its behavior version.

    This is process provenance. Generation audit events take retrieval
    identity from the selected IndexGeneration, rather than inferring it
    from the generation pod's settings. Medical handlers remain held back.
    """

    env: str
    service: str
    image_sha: str
    chat_deployment: str
    embed_version: str
    prompt_bundle_sha: str
    classifier_version: str
    image_digest: str = "unknown"
    release_bundle_sha: str = "unknown"
    chat_model_name: str = "unknown"
    chat_model_version: str = "unknown"
    embed_model_name: str = "unknown"
    embed_model_version: str = "unknown"
    deployment_revision: str = "unknown"

    @classmethod
    def from_settings(cls, s: Settings) -> Provenance:
        return cls(
            env=s.env,
            service=s.service_name,
            image_sha=s.image_sha,
            chat_deployment=s.chat_deployment,
            embed_version=s.embed_version,
            prompt_bundle_sha=s.prompt_bundle_sha,
            classifier_version=s.table_classifier_version,
            image_digest=s.image_digest,
            release_bundle_sha=s.release_bundle_sha,
            chat_model_name=s.chat_model_name,
            chat_model_version=s.chat_model_version,
            embed_model_name=s.embed_model_name,
            embed_model_version=s.embed_model_version,
            deployment_revision=s.deployment_revision,
        )

    def as_dict(self) -> dict[str, str]:
        """For the audit INSERT."""
        return asdict(self)

    def as_metric_dimensions(self) -> dict[str, str]:
        """For metric attributes and log records.

        A deliberately narrower set than the audit row. Every distinct
        combination of dimension values is a separate time series, so a
        high-cardinality dimension multiplies storage and query cost - and
        `image_sha` changes on every single deploy. It belongs in the audit
        trail, where it is one column on one row, and not on a metric, where it
        would create a new series per release forever.
        """
        return {
            "env": self.env,
            "service": self.service,
            "chat_deployment": self.chat_deployment,
            "prompt_bundle_sha": self.prompt_bundle_sha,
        }


# Adding another axis needs a field here, its from_settings mapping, and a
# decision about whether it belongs in as_metric_dimensions. That decision -
# is this worth a new time series on every value - is the one thing that
# should not be automatic.
