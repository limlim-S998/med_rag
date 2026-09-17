"""Legal transitions shared by immediate and scheduled durable jobs.

The same rules apply to Cosmos persistence and offline test stores. Persisted
stage names describe operations, not a particular model provider.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    scheduled = "scheduled"        # Preserved source, awaiting Airflow admission.
    queued = "queued"
    extracting = "extracting"
    classifying = "classifying"
    chunking = "chunking"
    annotating = "annotating"
    embedding = "embedding"
    indexing = "indexing"          # Qdrant upsert + Cognitive Search upload
    done = "done"
    failed = "failed"
    superseded = "superseded"      # A newer revision was already published.


# The pipeline is linear, so the interesting content of this map is what it
# does NOT contain: there is no edge from `queued` to `indexing`, so a bug that
# skipped extraction cannot silently produce an indexed document with no
# parsed artefact behind it.
#
# Every stage may go to `failed`. Nothing may leave `done`; a completed job is
# re-run by creating a new one, so the history of what happened is preserved
# rather than overwritten.
_LINEAR = [
    JobState.queued, JobState.extracting, JobState.classifying,
    JobState.chunking, JobState.annotating, JobState.embedding,
    JobState.indexing, JobState.done,
]

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    state: frozenset({_LINEAR[i + 1], JobState.failed})
    for i, state in enumerate(_LINEAR[:-1])
}
TRANSITIONS[JobState.done] = frozenset()
# Transient failures resume saved checkpoints within the attempt limit.
# Once a job reaches `failed`, resubmission creates a new history.
TRANSITIONS[JobState.failed] = frozenset()
TRANSITIONS[JobState.scheduled] = frozenset({JobState.queued, JobState.failed})
TRANSITIONS[JobState.queued] |= {JobState.superseded}
TRANSITIONS[JobState.superseded] = frozenset()

TERMINAL = frozenset({JobState.done, JobState.failed, JobState.superseded})
PROCESSING_STATES = tuple(_LINEAR[1:-1])


class IllegalTransition(Exception):
    """Raised instead of silently accepting a state jump.

    Silently accepting is the tempting option - the job carries on and nothing
    appears broken. It is also how a document ends up in the index having
    skipped annotation, which shows up much later as a study whose sparse half
    matches nothing.
    """

    def __init__(self, frm: JobState, to: JobState):
        allowed = ", ".join(sorted(TRANSITIONS[frm])) or "nothing (terminal)"
        super().__init__(f"{frm} -> {to} is not a legal transition; allowed: {allowed}")
        self.frm, self.to = frm, to


def check_transition(frm: JobState, to: JobState) -> None:
    if to not in TRANSITIONS[frm]:
        raise IllegalTransition(frm, to)


# Future remote-model retry policy, retained for the intended integrations.
# The installed placeholders use IngestionRunner's bounded retry/checkpoint
# handling; this policy is not the current provider selection or cost model.
#
# The retry decision is not "how many times" - it is "is this stage safe and
# cheap to repeat". Those are different questions per stage and the answers
# are not obvious, which is why they are written down rather than left to
# whoever is on call.
#
#   attempts  how many times to retry within a run
#   cost      what repeating it spends. `money` means a per-call charge that
#             recurs: Document Intelligence bills per page, and a TFL package
#             is hundreds of pages.
#   idempotent  whether repeating it converges or duplicates. Everything
#               downstream of chunking is keyed by deterministic chunk IDs, so
#               it converges - that property is what makes blind re-runs safe.

RETRY_COST: dict[JobState, dict] = {
    JobState.extracting: {
        "attempts": 1, "cost": "money", "idempotent": True,
        "note": "per-page Document Intelligence charge. Output is cached to "
                "Blob under the parser version BEFORE this transition is "
                "recorded, so a later stage failing never re-buys extraction.",
    },
    JobState.classifying: {
        "attempts": 3, "cost": "cpu", "idempotent": True,
        "note": "local sklearn model, pinned version. Deterministic: same "
                "input, same label, every time.",
    },
    JobState.chunking: {
        "attempts": 3, "cost": "cpu", "idempotent": True,
        "note": "pure function over cached layout JSON. Free to repeat.",
    },
    JobState.annotating: {
        "attempts": 3, "cost": "quota", "idempotent": True,
        "note": "Azure Language F0 has a monthly record cap; repeating eats it.",
    },
    JobState.embedding: {
        "attempts": 5, "cost": "money", "idempotent": True,
        "note": "AOAI tokens, and the deployment quota is shared with the "
                "live generation path - so retries here can starve user "
                "requests. Most attempts of any stage because 429 is the "
                "expected failure and backing off is the correct response.",
    },
    JobState.indexing: {
        "attempts": 3, "cost": "free", "idempotent": True,
        "note": "upserts keyed by deterministic chunk ID into both stores. "
                "Converges rather than duplicating, which is the whole reason "
                "the IDs are a pure function of the content path.",
    },
}


def attempts_for(state: JobState) -> int:
    return RETRY_COST.get(state, {}).get("attempts", 1)


def is_expensive(state: JobState) -> bool:
    """Stages that cost real money to repeat.

    Used to decide whether to resume a failed job from its last state or start
    over. Starting over is simpler and usually right - unless it re-buys
    extraction for a 300-page package.
    """
    return RETRY_COST.get(state, {}).get("cost") == "money"
