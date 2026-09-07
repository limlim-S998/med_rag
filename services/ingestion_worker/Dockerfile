FROM python:3.11-slim AS base
WORKDIR /app
# Install the shared lib first so it caches independently of service code.
COPY libs/medw_core /app/libs/medw_core
COPY services/ingestion_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e ./libs/medw_core
COPY services/ingestion_worker/app /app/app
EXPOSE 8000
# One worker per pod; scale with replicas, not with worker processes. Two
# knobs that both mean "more concurrency" is one knob too many.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
