SHELL := /bin/bash
export PATH := $(CURDIR)/.venv/bin:$(CURDIR)/data/tools/bin:$(PATH)
PYTHON ?= .venv/bin/python
TF ?= terraform
ENV ?= dev
TFVARS ?= $(abspath infra/terraform/environments/$(ENV).tfvars.json)
BOOTSTRAP_VARS ?= $(abspath data/terraform/bootstrap.tfvars.json)
BACKEND_CONFIG ?= $(abspath data/terraform/backend.hcl)
TF_PLAN ?= $(abspath data/terraform/$(ENV).tfplan)
RESOURCES ?= data/terraform/resources.json
KUBECONFIG ?= $(CURDIR)/data/terraform/kubeconfig
export KUBECONFIG
HOURS ?= 4
BUDGET_AUD ?= 20
MARKERS ?= azure and not recovery and not load
PROCESSING ?= immediate
AGENT_POOL ?= Azure Pipelines

.PHONY: dev test arch types lint chart-deps charts terraform-check check fmt eval images release migrate

dev:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements-dev.txt

test: chart-deps
	$(PYTHON) -m pytest tests services -q

arch:
	lint-imports --config .importlinter

types:
	mypy libs/medw_core

lint:
	ruff check .

chart-deps:
	@helm repo add medw-airflow https://airflow.apache.org --force-update >/dev/null
	@helm repo add qdrant https://qdrant.github.io/qdrant-helm --force-update >/dev/null
	@set -eu; for c in deploy/charts/*/; do \
	  [ "$$(basename $$c)" = medw-lib ] && continue; helm dependency build "$$c" >/dev/null; \
	done

charts: chart-deps
	@set -eu; for c in deploy/charts/*/; do \
	  n=$$(basename "$$c"); [ "$$n" = medw-lib ] && continue; \
	  helm lint "$$c" >/dev/null; helm template "$$n" "$$c" >/dev/null; echo "  ok   $$n"; \
	done
	@set -eu; for e in base dev staging prod platform/controllers platform/configuration secrets/dev clusters/dev; do \
	  kubectl kustomize "deploy/flux/$$e" >/dev/null; echo "  ok   flux/$$e"; \
	done
	$(PYTHON) scripts/check_ingress.py

terraform-check:
	$(TF) fmt -check -recursive infra/terraform
	$(TF) -chdir=infra/terraform/bootstrap init -backend=false -input=false -lockfile=readonly
	$(TF) -chdir=infra/terraform/bootstrap validate
	$(TF) -chdir=infra/terraform/environment init -backend=false -input=false -lockfile=readonly
	$(TF) -chdir=infra/terraform/environment validate
	$(TF) -chdir=infra/terraform/environment test

check: lint arch types test charts

fmt:
	ruff check --fix .
	ruff format .
	$(TF) fmt -recursive infra/terraform

eval:
	$(PYTHON) evals/run_retrieval_eval.py

images: ## Host image checks; BuildKit owns scheduling. No deployment or publishing.
	docker buildx bake --load
	@set -eu; for service in gateway retrieval generation ingestion-worker reranker airflow; do \
	  $(PYTHON) scripts/smoke_image.py --image "medw-$$service:scaffold-local" --service "$$service"; \
	done

release: ## Select existing exact artifacts. BUNDLE and ENV are explicit.
	@test -n "$(BUNDLE)" || (echo 'BUNDLE required'; exit 2)
	$(PYTHON) scripts/release.py select "$(BUNDLE)" --environment "$(ENV)"

migrate:
	bash scripts/migrate.sh "$(BUNDLE)" "$(REGISTRY)"

.PHONY: bootstrap-plan bootstrap-apply azure-init azure-preflight azure-plan azure-up azure-outputs azure-release azure-verify azure-down api-run
bootstrap-plan:
	@mkdir -p data/terraform
	$(TF) -chdir=infra/terraform/bootstrap init -input=false
	$(TF) -chdir=infra/terraform/bootstrap plan -input=false -var-file="$(BOOTSTRAP_VARS)" -out="$(abspath data/terraform/bootstrap.tfplan)"

bootstrap-apply:
	$(TF) -chdir=infra/terraform/bootstrap apply -input=false "$(abspath data/terraform/bootstrap.tfplan)"

azure-init:
	$(TF) -chdir=infra/terraform/environment init -input=false -backend-config="$(BACKEND_CONFIG)" -backend-config="key=$(ENV).tfstate"

azure-preflight:
	@mkdir -p data/terraform
	@umask 077; $(TF) -chdir=infra/terraform/environment show -json > data/terraform/state-inspection.json
	$(PYTHON) scripts/azure_preflight.py --config "$(TFVARS)" --state-json data/terraform/state-inspection.json --hours "$(HOURS)" --budget-aud "$(BUDGET_AUD)"

azure-plan: azure-preflight
	$(TF) -chdir=infra/terraform/environment plan -input=false -var-file="$(TFVARS)" -out="$(TF_PLAN)"

azure-up: ## Apply the saved, reviewed Azure plan; release delivery is separate.
	$(TF) -chdir=infra/terraform/environment apply -input=false "$(TF_PLAN)"

azure-outputs:
	@mkdir -p data/terraform
	@umask 077; $(TF) -chdir=infra/terraform/environment output -json resources > "$(RESOURCES)"
	$(TF) -chdir=infra/terraform/environment output -raw cluster_config > "deploy/flux/clusters/$(ENV)/platform-config.yaml"
	$(TF) -chdir=infra/terraform/environment output -raw flux_bootstrap > "deploy/flux/clusters/$(ENV)/flux-system/kustomization.yaml"

azure-release: ## Queue the existing application pipeline; uses az devops defaults from setup.
	az pipelines run --name "medw-$(ENV)-delivery" --parameters agentPool="$(AGENT_POOL)"

azure-verify: ## Readiness/workflow evidence; opt into recovery/load with MARKERS=azure.
	@test -n "$(STUDY)" -a -n "$(SECTION)" || (echo 'STUDY and SECTION required'; exit 2)
	$(PYTHON) -m pytest tests/integration -q -m '$(MARKERS)' --azure-resources="$(RESOURCES)" --azure-kubeconfig="$(KUBECONFIG)" --azure-study="$(STUDY)" --azure-section="$(SECTION)"

api-run:
	@test -n "$(FILE)" -a -n "$(STUDY)" -a -n "$(SECTION)" || (echo 'FILE, STUDY and SECTION required'; exit 2)
	$(PYTHON) scripts/api.py --resources "$(RESOURCES)" --study "$(STUDY)" --section "$(SECTION)" --file "$(FILE)" --processing "$(PROCESSING)"

azure-down: ## Destroy environment resources and their data; bootstrap/supplied accounts remain.
	$(TF) -chdir=infra/terraform/environment plan -destroy -input=false -var-file="$(TFVARS)" -out="$(TF_PLAN)"
	$(TF) -chdir=infra/terraform/environment apply -input=false "$(TF_PLAN)"
