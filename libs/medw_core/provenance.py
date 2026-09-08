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

    Every field is something that, if it changed, would change the output:

      image_sha         the code
      chat_deployment   the model, including its date suffix
      embed_version     the vector space the retrieved context came from
      prompt_bundle_sha the instructions
      classifier_ver    which template the numeric spine used

    `env` and `service` are not version axes - they are the *where*, and they
    are here because an audit row without them cannot be traced to a cluster.
    """

    env: str
    service: str
    image_sha: str
    chat_deployment: str
    embed_version: str
    prompt_bundle_sha: str
    classifier_version: str

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


# Adding a fourth axis is one field here, one line in from_settings, and a
# decision about whether it belongs in as_metric_dimensions. That decision -
# is this worth a new time series on every value - is the one thing that
# should not be automatic.
