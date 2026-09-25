variable "REGISTRY" { default = "" }
variable "TAG" { default = "scaffold-local" }
variable "SOURCE_SHA" { default = "unversioned" }

group "default" { targets = ["services"] }

// BuildKit schedules the shared build graph. Publishing is separate so every
// actual image can be smoke-tested before the release is selected.
target "services" {
  matrix = {
    service = [
      { name = "gateway", directory = "gateway" },
      { name = "retrieval", directory = "retrieval" },
      { name = "generation", directory = "generation" },
      { name = "ingestion-worker", directory = "ingestion_worker" },
      { name = "reranker", directory = "reranker" },
      { name = "airflow", directory = "airflow" }
    ]
  }
  name = service.name
  context = "."
  dockerfile = "services/${service.directory}/Dockerfile"
  tags = ["${REGISTRY != "" ? "${REGISTRY}/${service.name}" : "medw-${service.name}"}:${TAG}"]
  args = { SOURCE_SHA = SOURCE_SHA }
}
