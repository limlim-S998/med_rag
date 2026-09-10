FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
WORKDIR /app
# Runtime SQL access requires the Microsoft ODBC driver.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18=18.5.1.1-1 \
 && rm -rf /var/lib/apt/lists/*
COPY services/gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt \
 && pip install --no-cache-dir setuptools==80.9.0 wheel==0.45.1
COPY libs/medw_core /app/libs/medw_core
RUN pip install --no-cache-dir --no-deps --no-build-isolation /app/libs/medw_core
COPY services/gateway/app /app/app
ARG SOURCE_SHA=unversioned
ENV MEDW_BUILD_SOURCE_SHA=$SOURCE_SHA
LABEL org.opencontainers.image.revision=$SOURCE_SHA
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
