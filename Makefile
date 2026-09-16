.PHONY: dev up up-full up-legacy down eval fmt test arch types lint check chart-deps charts release migrate search-index seed

dev:           ## create .venv and install everything needed for host-side work
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt
	@echo
	@echo "Done. Activate with:  source .venv/bin/activate"
	@echo "Then point your editor at ./.venv so it can resolve medw_core."

up:            ## local stack: qdrant + reranker + retrieval
	docker compose up --build

up-full:       ## + gateway, generation and ingestion using durable local adapters
	docker compose --profile full up --build

up-legacy:     ## + chroma, so the "we replaced it" story is runnable
	docker compose --profile full --profile legacy up --build

down:
	docker compose --profile full --profile legacy down

test: chart-deps
	pytest tests services -q

arch:          ## the boundaries, enforced. Fails on a violation.
	lint-imports --config .importlinter

types:
	mypy libs/medw_core

lint:
	ruff check .

chart-deps:    ## prepare local library dependencies before tests render charts
	@set -eu; for c in deploy/charts/*/; do \
	  [ "$$(basename $$c)" = "medw-lib" ] && continue; \
	  helm dependency build $$c >/dev/null; \
	done

charts: chart-deps ## fail if any chart or Flux environment cannot render
	@set -eu; for c in deploy/charts/*/; do \
	  n=$$(basename $$c); [ "$$n" = "medw-lib" ] && continue; \
	  helm lint $$c >/dev/null; \
	  helm template $$n $$c >/dev/null; echo "  ok   $$n"; \
	  if [ -f "$${c}values-local.yaml" ]; then \
	    helm template $$n $$c -f "$${c}values-local.yaml" >/dev/null; \
	  fi; \
	done
	@set -eu; for e in base dev staging prod local; do \
	  kubectl kustomize deploy/flux/$$e >/dev/null; echo "  ok   flux/$$e"; \
	done
	@python scripts/check_ingress.py

release:       ## BUNDLE=path ENV=dev; selects exact artifacts, never builds or pushes
	@test -n "$(BUNDLE)" -a -n "$(ENV)" || (echo "BUNDLE and ENV required"; exit 2)
	python scripts/release.py select "$(BUNDLE)" --environment "$(ENV)"

check: lint arch types test charts   ## host checks; image startup checks run separately

fmt:
	ruff check --fix . && ruff format .

eval:          ## recall@k against the golden set
	python evals/run_retrieval_eval.py

migrate:       ## migration identity in MEDW_SQL_CONNECTION_STRING
	python scripts/migrate.py

search-index:  ## push the Cognitive Search index definition
	az rest --method put \
	  --uri "$(MEDW_SEARCH_ENDPOINT)/indexes/csr-chunks?api-version=2024-07-01" \
	  --resource https://search.azure.com \
	  --body @infra/search/csr-chunks-index.json

seed:          ## parse + chunk + index the sample study
	python -m pipelines.cli index --study ABC-101 --path data/sample/

.PHONY: demo-run
demo-run:      ## Upload FILE through the normal Azure application and retain evidence
	@test -n "$(FILE)" || (echo "FILE is required"; exit 2)
	./.venv/bin/python scripts/demo_run.py --config "$(AZURE_CONFIG)" --file "$(FILE)"

# Copy infra/azure.example.json to this ignored path and fill its resource IDs.
AZURE_CONFIG ?= data/azure/config.json
AZURE_PYTHON ?= .venv/bin/python
.PHONY: azure-preflight azure-up azure-verify azure-down
azure-preflight: ## cloud/access/price checks and a cached temporary CI probe; no paid Azure creation
	$(AZURE_PYTHON) scripts/azure.py preflight --config "$(AZURE_CONFIG)"

azure-up: ## resumable Azure provisioning, identities, pipeline and Flux deployment
	$(AZURE_PYTHON) scripts/azure.py up --config "$(AZURE_CONFIG)"

azure-verify: ## export actual cluster/TLS/route/release evidence
	$(AZURE_PYTHON) scripts/azure.py verify --config "$(AZURE_CONFIG)"

azure-down: ## remove only journalled owned infrastructure and borrowed test data
	$(AZURE_PYTHON) scripts/azure.py down --config "$(AZURE_CONFIG)"
