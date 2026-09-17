# Official Airflow distribution; the DAG is an immutable release artifact.
FROM apache/airflow:3.3.1-python3.11@sha256:9417c2bba39de23e375f1fe98f284aee1561cedafd521edf1171b26c3b1e46e5
ARG SOURCE_SHA=unversioned
LABEL org.opencontainers.image.revision=$SOURCE_SHA
ENV MEDW_BUILD_SOURCE_SHA=$SOURCE_SHA PYTHONPATH=/opt/airflow
COPY --chown=airflow:root services/airflow/requirements.txt /opt/airflow/requirements-medw.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /opt/airflow/requirements-medw.txt && pip check
COPY --chown=airflow:root pipelines/__init__.py pipelines/batch_client.py /opt/airflow/pipelines/
COPY --chown=airflow:root pipelines/dags/ingest_study.py /opt/airflow/dags/ingest_study.py
