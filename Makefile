.PHONY: dev up up-full up-legacy down eval fmt test arch types lint check migrate search-index seed

dev:           ## create .venv and install everything needed for host-side work
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt
	@echo
	@echo "Done. Activate with:  source .venv/bin/activate"
	@echo "Then point your editor at ./.venv so it can resolve medw_core."

up:            ## local stack: qdrant + reranker + retrieval
	docker compose up --build

up-full:       ## + gateway, generation, ingestion, and the store emulators
	docker compose --profile full up --build

up-legacy:     ## + chroma, so the "we replaced it" story is runnable
	docker compose --profile full --profile legacy up --build

down:
	docker compose down -v

test:
	pytest tests services -q

arch:          ## the boundaries, enforced. Fails on a violation.
	lint-imports --config .importlinter

types:
	mypy libs/medw_core

lint:
	ruff check .

check: lint arch types test   ## everything CI runs, in the same order

fmt:
	ruff check --fix . && ruff format .

eval:          ## recall@k against the golden set
	python evals/run_retrieval_eval.py

migrate:       ## apply db/sql in order. Additive-only in anything but dev.
	sqlcmd -G -S $(MEDW_SQL_SERVER) -d $(MEDW_SQL_DATABASE) -i db/sql/0001_core.sql
	sqlcmd -G -S $(MEDW_SQL_SERVER) -d $(MEDW_SQL_DATABASE) -i db/sql/0002_audit.sql
	sqlcmd -G -S $(MEDW_SQL_SERVER) -d $(MEDW_SQL_DATABASE) -i db/sql/0003_grants.sql

search-index:  ## push the Cognitive Search index definition
	az rest --method put \
	  --uri "$(MEDW_SEARCH_ENDPOINT)/indexes/csr-chunks?api-version=2024-07-01" \
	  --resource https://search.azure.com \
	  --body @infra/search/csr-chunks-index.json

seed:          ## parse + chunk + index the sample study
	python -m pipelines.cli index --study ABC-101 --path data/sample/
