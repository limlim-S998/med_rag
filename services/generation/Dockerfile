FROM python:3.11-slim AS base
WORKDIR /app
# The one service that needs a system dependency: Azure SQL over AAD goes
# through ODBC, so the Microsoft driver has to be in the image. This is also
# why the audit write lives in generation and not in every service - one
# image carrying an apt repository is a tolerable cost, four is not.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
      > /etc/apt/sources.list.d/mssql.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && rm -rf /var/lib/apt/lists/*

# Install the shared lib first so it caches independently of service code.
COPY libs/medw_core /app/libs/medw_core
COPY services/generation/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e ./libs/medw_core
COPY services/generation/app /app/app
# The prompt bundle is baked into the image, and its SHA is passed as a Helm
# value. Mounting prompts from a ConfigMap would let them change without an
# image change - which is precisely the silent behaviour change the version
# axes exist to prevent.
EXPOSE 8000
# One worker per pod; scale with replicas, not with worker processes. Two
# knobs that both mean "more concurrency" is one knob too many.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
