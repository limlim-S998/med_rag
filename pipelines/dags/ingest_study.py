"""Nightly, finite batches of documents explicitly submitted for deferred work."""

import os
from datetime import timedelta

import pendulum
from airflow.exceptions import AirflowException
from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk import dag, get_current_context, task
from airflow.timetables.trigger import CronTriggerTimetable

from pipelines.batch_client import BatchClient


def finished(batch_id: str) -> bool:
    with BatchClient() as client:
        return client.status(batch_id)["status"] in {"completed", "failed"}


@dag(
    dag_id="ingest_study",
    schedule=CronTriggerTimetable(os.getenv("MEDW_BATCH_SCHEDULE", "0 2 * * *"),
                                 timezone=os.getenv("MEDW_BATCH_TIMEZONE", "Australia/Brisbane")),
    start_date=pendulum.datetime(2025, 1, 1, tz="Australia/Brisbane"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=2,
    dagrun_timeout=timedelta(hours=8),
    default_args={"retries": 3, "retry_delay": timedelta(seconds=30)},
    tags=["ingestion", "nightly"],
)
def ingest_study():
    @task
    def select_and_admit() -> str:
        context = get_current_context()
        # Logical time stays fixed across retries; missed nights have no lower cutoff.
        boundary = context.get("data_interval_end") or context["dag_run"].run_after
        with BatchClient() as client:
            result = client.prepare(context["run_id"], boundary.timestamp())
        return result["id"]

    @task
    def report(batch_id: str) -> dict:
        with BatchClient() as client:
            result = client.status(batch_id)
        if result["status"] != "completed":
            raise AirflowException(f"Batch {batch_id} failed: {result['counts']}")
        return {"id": batch_id, "counts": result["counts"],
                "deferred_by_limit": result["deferred_by_limit"]}

    batch_id = select_and_admit()
    wait = PythonSensor(task_id="wait_for_documents", python_callable=finished,
                        op_kwargs={"batch_id": batch_id}, mode="reschedule",
                        poke_interval=30, timeout=7 * 60 * 60)
    batch_id >> wait >> report(batch_id)


ingest_study()
