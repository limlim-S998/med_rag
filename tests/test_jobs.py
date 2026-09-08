# The ingestion state machine, and the properties recovery depends on.
#
# "It failed somewhere in ingestion" is not an operable message. These tests
# assert the two things that make it operable: illegal paths are impossible,
# and the cost of redoing each stage is written down rather than folklore.

from itertools import pairwise

import pytest

from medw_core.jobs import (
    RETRY_COST,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    JobState,
    attempts_for,
    check_transition,
    is_expensive,
)
from medw_core.local.jobs import InMemoryJobStore

PIPELINE = [
    JobState.queued, JobState.extracting, JobState.classifying,
    JobState.chunking, JobState.annotating, JobState.embedding,
    JobState.indexing, JobState.done,
]


def test_the_happy_path_is_legal_end_to_end():
    for frm, to in pairwise(PIPELINE):
        check_transition(frm, to)


def test_stages_cannot_be_skipped():
    """The point of the map is what it omits.

    A jump from queued to indexing would produce an indexed document with no
    parsed artefact behind it — retrievable, and backed by nothing.
    """
    with pytest.raises(IllegalTransition):
        check_transition(JobState.queued, JobState.indexing)
    with pytest.raises(IllegalTransition):
        check_transition(JobState.chunking, JobState.done)


def test_the_pipeline_cannot_run_backwards():
    with pytest.raises(IllegalTransition):
        check_transition(JobState.indexing, JobState.embedding)


@pytest.mark.parametrize("state", [s for s in JobState if s not in TERMINAL])
def test_every_live_stage_can_fail(state):
    """A stage that could not fail would need its errors swallowed somewhere."""
    assert JobState.failed in TRANSITIONS[state]


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_terminal_states_are_terminal(state):
    """Nothing leaves done or failed. A completed job is re-run by creating a
    new one, so the record of what happened survives instead of being
    overwritten by the retry."""
    assert TRANSITIONS[state] == frozenset()


def test_the_error_names_what_was_allowed():
    """An exception saying only "illegal transition" sends you to the source.
    Naming the legal options answers the question in the log line."""
    with pytest.raises(IllegalTransition, match="allowed: extracting, failed"):
        check_transition(JobState.queued, JobState.indexing)


# --- the store enforces the shared rules ---------------------------------


async def test_store_rejects_an_illegal_advance():
    """The local store must not be more permissive than Cosmos. If it were, a
    job could take a path locally that the real store rejects — and that only
    surfaces in the environment you cannot debug."""
    store = InMemoryJobStore()
    job = await store.create("ABC-101", "doc-1")
    with pytest.raises(IllegalTransition):
        await store.advance(job, JobState.indexing)


async def test_store_walks_the_whole_pipeline():
    store = InMemoryJobStore()
    job = await store.create("ABC-101", "doc-1")
    for state in PIPELINE[1:]:
        job = await store.advance(job, state)
    assert job["state"] == JobState.done
    assert job["history"] == [str(s) for s in PIPELINE]


async def test_failure_records_which_stage_died():
    """Which stage failed decides whether a retry is free or re-buys a
    per-page extraction charge. A job that only knows it failed cannot answer
    that."""
    store = InMemoryJobStore()
    job = await store.create("ABC-101", "doc-1")
    job = await store.advance(job, JobState.extracting)
    job = await store.fail(job, JobState.extracting, "DI timed out")
    assert job["state"] == JobState.failed
    assert job["failed_at_state"] == JobState.extracting
    assert "DI timed out" in job["error"]


# --- retry economics -----------------------------------------------------


@pytest.mark.parametrize("state", sorted(RETRY_COST))
def test_every_retryable_stage_states_its_cost(state):
    entry = RETRY_COST[state]
    assert entry["cost"] in {"free", "cpu", "quota", "money"}
    assert entry["attempts"] >= 1
    assert entry["note"], f"{state} has no explanation of what a re-run costs"


def test_every_non_terminal_stage_after_queued_has_a_retry_policy():
    """A stage with no policy gets whatever the default is, which is exactly
    the kind of implicit decision this table exists to remove."""
    needs = {s for s in JobState if s not in TERMINAL and s is not JobState.queued}
    assert needs <= RETRY_COST.keys()


def test_extraction_is_the_stage_you_do_not_repeat():
    """Document Intelligence bills per page and a TFL package is hundreds. It
    gets the fewest attempts of any stage for that reason alone."""
    assert is_expensive(JobState.extracting)
    assert attempts_for(JobState.extracting) == 1
    assert attempts_for(JobState.extracting) < attempts_for(JobState.embedding)


def test_embedding_gets_the_most_attempts():
    """429 is the expected failure there, and backing off is the correct
    response rather than an error."""
    assert attempts_for(JobState.embedding) == max(
        attempts_for(s) for s in RETRY_COST
    )


def test_every_stage_is_idempotent():
    """The property the whole recovery model rests on: a blind re-run
    converges rather than duplicating. It is bought by the deterministic chunk
    IDs, and if any stage lost it, re-running a DAG after a parser fix would
    stop being safe."""
    assert all(entry["idempotent"] for entry in RETRY_COST.values())
