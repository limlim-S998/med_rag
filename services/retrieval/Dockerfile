FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
WORKDIR /app
COPY services/retrieval/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt \
 && pip install --no-cache-dir setuptools==80.9.0 wheel==0.45.1
COPY libs/medw_core /app/libs/medw_core
RUN pip install --no-cache-dir --no-deps --no-build-isolation /app/libs/medw_core
COPY services/retrieval/app /app/app
ARG SOURCE_SHA=unversioned
ENV MEDW_BUILD_SOURCE_SHA=$SOURCE_SHA
LABEL org.opencontainers.image.revision=$SOURCE_SHA
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
