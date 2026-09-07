.PHONY: dev up up-full up-legacy down eval fmt test arch types lint check charts release migrate search-index seed

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

charts:        ## render + lint every chart, the way CI does
	@for c in deploy/charts/*/; do \
	  n=$$(basename $$c); [ "$$n" = "medw-lib" ] && continue; \
	  helm dependency update $$c >/dev/null && helm lint $$c >/dev/null \
	    && helm template $$n $$c >/dev/null && echo "  ok   $$n" || echo "  FAIL $$n"; \
	done
	@for e in dev staging prod; do \
	  kubectl kustomize deploy/flux/$$e >/dev/null && echo "  ok   flux/$$e" || echo "  FAIL flux/$$e"; \
	done

release:       ## what the pipeline does: set every image tag to HEAD. DRY=1 to preview.
	python scripts/bump_image_tag.py --all --tag $$(git rev-parse HEAD) $(if $(DRY),--dry-run,)

check: lint arch types test charts   ## everything CI runs, in the same order

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
