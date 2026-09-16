#!/usr/bin/env python3
"""Provision the ordinary Azure deployment, with a resumable ownership journal.

No credentials belong in the configuration or evidence. The Azure CLI supplies
Azure access; MEDW_DEVOPS_PAT can optionally supply Azure DevOps access. Paid
creation is refused until preflight passes. Files under data/azure are local
operator state, including the cluster kubeconfig and client trust certificate.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import partial
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")
PROVIDERS = ("Microsoft.ContainerService", "Microsoft.ContainerRegistry", "Microsoft.Storage",
             "Microsoft.Sql", "Microsoft.DocumentDB", "Microsoft.Search", "Microsoft.Insights",
             "Microsoft.OperationalInsights", "Microsoft.ManagedIdentity", "Microsoft.Network", "Microsoft.Compute")
API = "https://management.azure.com"


class SetupError(RuntimeError):
    pass


def evidence_error(exc: Exception) -> dict:
    """CLI stderr can include credentials; evidence stores only safe categories."""
    result: dict = {"type": type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        result["http_status"] = exc.code
    return result


def helm_apply_options(version: str) -> list[str]:
    """Preserve AKS-injected webhook selectors when rerunning platform setup."""
    match = re.match(r"v?(\d+)\.", version.strip())
    if not match:
        raise SetupError("Cannot determine Helm major version")
    # Helm 3 uses a three-way client merge. Helm 4 defaults to server apply,
    # which conflicts with fields owned by AKS admissionsenforcer on reruns.
    return ["--server-side=false"] if int(match[1]) >= 4 else []


def run(args: list[str], *, payload: str | None = None, env: dict | None = None,
        json_result: bool = False, missing_ok: bool = False) -> Any:
    result = subprocess.run(args, input=payload, text=True, capture_output=True,
                            env={**os.environ, **(env or {})}, check=False)
    if result.returncode:
        # CLI errors occasionally echo request bodies. Never include stdout or
        # request input; the command name and exit code suffice for secret calls.
        if missing_ok and any(marker in result.stderr for marker in (
            "ResourceNotFound", "ResourceGroupNotFound", "NotFound", "does not exist")):
            return None
        raise SetupError(f"{' '.join(args[:3])} failed ({result.returncode}): "
                         + result.stderr[-1600:])
    return json.loads(result.stdout) if json_result and result.stdout.strip() else result.stdout


def az(*args: str, missing_ok: bool = False, subscription: str | None = None) -> Any:
    return run(["az", *args, *(["--subscription", subscription] if subscription else []),
                "--only-show-errors", "-o", "json"],
               json_result=True, missing_ok=missing_ok)


def retry_rbac(operation, *, timeout: float = 300):
    """Wait only for documented role-propagation failures, not arbitrary errors."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return operation()
        except SetupError as exc:
            if not any(code in str(exc) for code in ("AuthorizationPermissionMismatch", "AuthorizationFailure",
                                                    "Forbidden", "does not have authorization")):
                raise
            if time.monotonic() >= deadline:
                raise SetupError("Azure role assignment did not propagate within five minutes") from exc
            time.sleep(10)


def http_json(url: str, *, token: str = "", method: str = "GET", body: Any = None,
              basic: bool = False) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = ("Basic " if basic else "Bearer ") + token
    request = urllib.request.Request(url, headers=headers, method=method,
                                     data=None if body is None else json.dumps(body).encode())
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if method == "GET" and exc.code == 429 and attempt < 3:
                try:
                    delay = float(exc.headers.get("Retry-After", 5 * 2 ** attempt))
                except (TypeError, ValueError):
                    delay = 5 * 2 ** attempt
                time.sleep(max(1, min(delay, 60)))
                continue
            # Do not leak HTTP request headers, tokens, or secret-bearing responses.
            raise SetupError(f"{method} {urllib.parse.urlsplit(url).path}: HTTP {exc.code}") from exc
    raise SetupError("HTTP retry budget exhausted")


def write_private(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def arm_parts(resource_id: str, provider: str, resource_type: str) -> tuple[str, str]:
    match = re.fullmatch(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
                         + re.escape(provider) + "/" + resource_type + r"/([^/]+)",
                         resource_id, re.IGNORECASE)
    if not match:
        raise SetupError(f"Expected an Azure {provider}/{resource_type} resource ID")
    return match[2], match[3]


def load_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text())
    required = ("resource_group", "name_prefix", "location", "environment", "devops")
    for key in required:
        if not config.get(key):
            raise SetupError(f"Configuration requires {key}")
    if config["environment"] != "dev":
        raise SetupError("This initial deployment supports the dev overlay only")
    if not re.fullmatch(r"[a-z][a-z0-9]{2,12}", config["name_prefix"]):
        raise SetupError("name_prefix must be 3-13 lowercase letters/digits")
    if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,80}", config["resource_group"]):
        raise SetupError("invalid resource_group")
    if not 0 < config.get("hours", 0) <= 24 or not 0 < config.get("budget_aud", 0) <= 20:
        raise SetupError("Initial exercise requires hours in (0,24] and budget_aud in (0,20]")
    if config.get("sql_admin_type", "User") not in {"User", "Group"}:
        raise SetupError("SQL administrator must be a User or Group")
    for key in ("search_index", "cosmos_database", "sql_database", "study_id", "section_path"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", config.get(key, "")):
            raise SetupError(f"invalid {key}")
    organization = config["devops"].get("organization", "")
    if organization and not re.fullmatch(r"https://dev.azure.com/[A-Za-z0-9_-]+/?", organization):
        raise SetupError("devops.organization must be https://dev.azure.com/ORGANIZATION")
    return config


def permission_allows(permissions: list[dict], action: str) -> bool:
    return any(any(fnmatch.fnmatchcase(action.lower(), pattern.lower())
                   for pattern in entry.get("actions", []))
               and not any(fnmatch.fnmatchcase(action.lower(), pattern.lower())
                           for pattern in entry.get("notActions", [])) for entry in permissions)


def quota_available(usages: list[dict], family: str, needed: int = 4) -> bool:
    matches = [item for item in usages if item.get("name", {}).get("value", "").lower()
               == family.lower()]
    return bool(matches) and int(matches[0]["limit"]) - int(matches[0]["currentValue"]) >= needed


def retail_prices(location: str) -> dict:
    """Fetch current AUD retail quotes; no hard-coded price presented as current."""
    filters = {
        "node": f"armRegionName eq '{location}' and armSkuName eq 'Standard_D4s_v5'",
        "registry": f"armRegionName eq '{location}' and serviceName eq 'Container Registry'",
        "sql": f"armRegionName eq '{location}' and productName eq 'SQL Database Single Basic'",
        "disk": f"armRegionName eq '{location}' and serviceName eq 'Storage' "
                "and productName eq 'Premium SSD Managed Disks'",
        "load_balancer": "armRegionName eq 'Global' and serviceName eq 'Load Balancer'",
        "public_ip": f"armRegionName eq '{location}' and serviceName eq 'Virtual Network'",
    }
    prices = {}
    for kind, query in filters.items():
        url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode({
            "currencyCode": "'AUD'", "$filter": query})
        items = []
        while url:
            page = http_json(url)
            items.extend(page.get("Items", []))
            url = page.get("NextPageLink", "")
        items = [row for row in items if row.get("type") == "Consumption"
                 and row.get("retailPrice", 0) > 0
                 and not any(word in json.dumps(row).lower() for word in
                             ("windows", "spot", "low priority", "reservation"))]
        predicates = {
            "node": lambda row: row.get("unitOfMeasure") == "1 Hour",
            "registry": lambda row: row.get("meterName") == "Basic Registry Unit",
            "sql": lambda row: row.get("meterName") == "B DTU",
            "disk": lambda row: row.get("meterName") == "P6 LRS Disk",
            "load_balancer": lambda row: row.get("meterName") == "Standard Included LB Rules and Outbound Rules",
            "public_ip": lambda row: "Standard IPv4" in row.get("meterName", ""),
        }
        candidates = [row for row in items if predicates[kind](row)]
        if not candidates:
            raise SetupError(f"No unambiguous AUD retail quote available for {kind}")
        row = min(candidates, key=lambda item: item["retailPrice"])
        unit = row["unitOfMeasure"]
        divisor = {"1 Hour": 1, "1/Month": 730, "1 Month": 730, "1/Day": 24,
                   "1 Day": 24}.get(unit)
        if divisor is None:
            raise SetupError(f"Unsupported retail unit for {kind}: {unit}")
        prices[kind] = {"hourly_aud": row["retailPrice"] / divisor,
                        "meter": row["meterName"], "price_aud": row["retailPrice"],
                        "unit": unit}
    return prices


def estimate_cost(prices: dict, hours: float) -> dict:
    # One 64Gi node OS disk + one conservative 64Gi quote for the 8Gi data disk.
    hourly = sum(prices[k]["hourly_aud"] * (2 if k in {"disk", "public_ip"} else 1) for k in prices)
    # Bounded tiny documents/traffic. Reserve AUD5 for Blob operations, egress,
    # monitoring ingestion, disk operations and price rounding/minimum billing.
    reserve = 5.0
    return {"currency": "AUD", "hours": hours, "hourly_aud": round(hourly, 4),
            "usage_reserve_aud": reserve, "estimated_aud": round(hourly * hours + reserve, 2),
            "quotes": prices, "is_hard_cap": False}


class Deployment:
    def __init__(self, config: dict, *, root: pathlib.Path = ROOT):
        self.config = copy.deepcopy(config)
        self.root = root
        self.directory = root / "data" / "azure" / config["resource_group"]
        self.journal_path = self.directory / "state.json"
        self.state = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
        self._resuming_pending = set(self.state.get("pending", {}))
        self._assert_compatible()

    def _assert_compatible(self):
        original = self.state.get("config")
        if not original or self.state.get("teardown_complete"):
            return
        immutable = ("resource_group", "name_prefix", "location", "environment", "borrowed_search_id",
                     "borrowed_cosmos_id", "search_subscription_id", "cosmos_subscription_id",
                     "cosmos_database", "search_index", "sql_database", "git_url", "git_branch",
                     "study_id", "section_path")
        for key in immutable:
            if (original.get(key) or "") != (self.config.get(key) or ""):
                raise SetupError(f"Existing deployment journal differs in {key}; finish cleanup with "
                                 "the original configuration or choose another resource_group")
        for key in ("subscription_id", "writer_object_id", "sql_admin_object_id"):
            if self.config.get(key) and original.get(key) != self.config[key]:
                raise SetupError(f"Existing deployment identity differs in {key}")
        for key in ("organization", "project", "pipeline_name", "azure_service_connection_name"):
            if original["devops"].get(key) != self.config["devops"].get(key):
                raise SetupError(f"Existing deployment journal differs in devops.{key}")

    def save(self) -> None:
        write_private(self.journal_path, json.dumps(self.state, indent=2) + "\n")

    def account(self) -> dict:
        account = az("account", "show")
        configured = self.config.get("subscription_id")
        if configured and configured != account["id"]:
            raise SetupError("Active Azure CLI subscription differs from subscription_id")
        if account["state"] != "Enabled":
            raise SetupError("Azure subscription is not enabled")
        self.config["subscription_id"] = account["id"]
        self.config["tenant_id"] = account["tenantId"]
        fingerprint = hashlib.sha256(
            (account["id"] + "/" + self.config["resource_group"]).encode()).hexdigest()[:8]
        self.config["prefix"] = self.config["name_prefix"] + fingerprint
        self.config["owner"] = fingerprint
        return account

    def devops(self, path: str, *, method: str = "GET", body: Any = None,
               project: bool = True) -> Any:
        settings = self.config["devops"]
        if not settings.get("organization") or not settings.get("project"):
            raise SetupError("Set devops.organization and devops.project in the configuration")
        parsed = urllib.parse.urlsplit(path)
        segments = parsed.path.split("/")
        mappings = {"projects": ("core", "projects", "projectId"),
                    "serviceendpoint": ("serviceendpoint", "endpoints", "endpointId"),
                    "distributedtask": ("distributedtask", "pools", "poolId"),
                    "pipelines": ("pipelines", "pipelines", "pipelineId"),
                    "build": ("build", "definitions", "definitionId")}
        if segments[0] not in mappings:
            raise SetupError("Unsupported DevOps API operation: " + segments[0])
        area, resource, identifier = mappings[segments[0]]
        index = 1 if segments[0] in {"projects", "pipelines"} else 2
        route = {"project": settings["project"]} if project else {}
        if len(segments) > index:
            route[identifier] = urllib.parse.unquote(segments[index])
        if segments[:2] == ["build", "builds"]:
            resource = "builds"
            route.pop("definitionId", None)
            if len(segments) > 2:
                route["buildId"] = segments[2]
        if "agents" in segments:
            resource = "agents"
        if "runs" in segments:
            resource = "runs"
            if segments[-1] != "runs":
                route["runId"] = segments[-1]
        if segments[:2] == ["pipelines", "pipelinepermissions"]:
            area = resource = "pipelinePermissions"
            route.pop("pipelineId", None)
            route.update(resourceType=segments[2], resourceId=segments[3])
        query = dict(urllib.parse.parse_qsl(parsed.query))
        version = re.sub(r"(-preview)\.\d+$", r"\1", query.pop("api-version", "7.1"))
        command = ["az", "devops", "invoke", "--organization", settings["organization"],
                   "--area", area, "--resource", resource, "--http-method", method,
                   "--api-version", version, "--only-show-errors", "-o", "json"]
        if route:
            command += ["--route-parameters", *(key + "=" + value for key, value in route.items())]
        if query:
            command += ["--query-parameters", *(key + "=" + value for key, value in query.items())]
        env = {}
        if os.environ.get("MEDW_DEVOPS_PAT"):
            env["AZURE_DEVOPS_EXT_PAT"] = os.environ["MEDW_DEVOPS_PAT"]
        # CLI selects the appropriate cached personal/work account credential;
        # using only the active Azure subscription's tenant token can return403.
        with tempfile.TemporaryDirectory(prefix="medw-devops-") as temp:
            if body is not None:
                request_path = pathlib.Path(temp) / "request.json"
                write_private(request_path, json.dumps(body))
                command += ["--in-file", str(request_path), "--encoding", "utf-8"]
            return run(command, env=env, json_result=True)

    def preflight(self) -> dict:
        report: dict = {"timestamp": dt.datetime.now(dt.UTC).isoformat(), "checks": [],
                        "passed": False, "billable_resources_created": False}

        def check(name, operation):
            try:
                detail = operation()
                report["checks"].append({"name": name, "passed": True, "detail": detail})
                return detail
            except (SetupError, OSError, ValueError, KeyError) as exc:
                report["checks"].append({"name": name, "passed": False, "detail": str(exc)})
                return None

        def tools():
            missing = [name for name in ("az", "docker", "kubectl", "helm", "flux", "git", "openssl")
                       if shutil.which(name) is None]
            if missing:
                raise SetupError("Missing commands: " + ", ".join(missing))
            run(["docker", "info", "--format", "{{.ServerVersion}}"])
            return "Required local tools and Docker available"

        check("tools", tools)
        if check("account", lambda: {key: self.account()[key] for key in ("id", "tenantId")}) is None:
            return report
        sub = self.config["subscription_id"]
        check("subscription-permissions", lambda: self._permissions(sub))
        check("providers", self._providers)
        check("node-quota", self._quota)
        check("borrowed-resources", self._borrowed)
        check("entra", self._entra_preflight)
        check("git", self._git_preflight)
        check("devops", self._devops_preflight)
        check("estimated-cost", self._cost)
        check("existing-deployment", self._assert_compatible)
        report["passed"] = all(row["passed"] for row in report["checks"])
        return report

    def _permissions(self, subscription):
        result = az("rest", "--url", f"{API}/subscriptions/{subscription}/providers/"
                    "Microsoft.Authorization/permissions?api-version=2022-04-01")["value"]
        needed = ["Microsoft.Resources/subscriptions/resourceGroups/write",
                  "Microsoft.Authorization/roleAssignments/write",
                  "Microsoft.ContainerService/managedClusters/write"]
        denied = [action for action in needed if not permission_allows(result, action)]
        if denied:
            raise SetupError("Missing resource permissions: " + ", ".join(denied))
        return "Resource creation and role-assignment permissions present"

    def _providers(self):
        pending = [name for name in PROVIDERS if az("provider", "show", "-n", name)
                   .get("registrationState") != "Registered"]
        if pending:
            raise SetupError("Register resource providers before paid creation: " + ", ".join(pending))
        return "Required resource providers registered"

    def _quota(self):
        usage = az("vm", "list-usage", "-l", self.config["location"])
        known = self.state.get("completed", {}).get("aks")
        if known:
            expected = (f"/subscriptions/{self.config['subscription_id']}/resourceGroups/"
                        f"{self.config['resource_group']}/providers/Microsoft.ContainerService/managedClusters/"
                        + self.config["prefix"] + "aks")
            group = az("group", "show", "-n", self.config["resource_group"])
            cluster = az("aks", "show", "-g", self.config["resource_group"],
                         "-n", self.config["prefix"] + "aks")
            if (known.get("id", "").lower() != expected.lower()
                    or cluster.get("id", "").lower() != expected.lower()
                    or group.get("tags", {}).get("medw-owner") != self.config["owner"]):
                raise SetupError("Existing AKS allocation does not match journalled ownership")
            pools = cluster.get("agentPoolProfiles", [])
            if (len(pools) != 1 or pools[0].get("vmSize") != "Standard_D4s_v5"
                    or pools[0].get("count") != 1 or pools[0].get("enableAutoScaling")
                    or pools[0].get("osDiskSizeGb") != 64
                    or cluster.get("sku", {}).get("tier", "").lower() != "free"):
                raise SetupError("Owned AKS cluster no longer matches the approved bounded sizing")
            if not quota_available(usage, "cores", 0) or not quota_available(usage, "standardDSv5Family", 0):
                raise SetupError("Usage API did not prove quota covers the existing owned AKS node")
            return "Owned one-node Standard_D4s_v5 cluster verified; no additional vCPUs required"
        if not quota_available(usage, "cores") or not quota_available(usage, "standardDSv5Family"):
            raise SetupError("Need 4 available regional and Standard DSv5 vCPUs; usage API "
                             "did not prove sufficient quota (no paid fallback is permitted)")
        skus = az("vm", "list-skus", "-l", self.config["location"], "--size", "Standard_D4s_v5",
                  "--all")
        if not any(not any(r.get("type") == "Location" for r in item.get("restrictions", []))
                   for item in skus):
            raise SetupError("Standard_D4s_v5 is restricted/unavailable for this subscription")
        return "Standard_D4s_v5 available; quota covers 4 vCPUs"

    def _borrowed(self):
        found = {}
        for key, provider, kind in (("borrowed_search_id", "Microsoft.Search", "searchServices"),
                                    ("borrowed_cosmos_id", "Microsoft.DocumentDB", "databaseAccounts")):
            resource_id = self.config.get(key)
            target_sub = (resource_id.split("/")[2] if resource_id else self.config.get(
                "search_subscription_id" if "search" in key else "cosmos_subscription_id")) or self.config["subscription_id"]
            account = az("account", "show", subscription=target_sub)
            if account["tenantId"] != self.config["tenant_id"]:
                raise SetupError("Storage/index subscriptions must share the configured Entra tenant")
            scoped_az = partial(az, subscription=target_sub)
            if not resource_id:
                command = ("search", "service", "list") if "search" in key else ("cosmosdb", "list")
                accounts = scoped_az(*command)
                if any((item.get("sku", {}).get("name", "").lower() == "free"
                        if "search" in key else item.get("enableFreeTier")) for item in accounts):
                    raise SetupError(f"Existing free-tier account found; configure {key} for reuse")
                registration = scoped_az("provider", "show", "-n", provider)
                if registration.get("registrationState") != "Registered":
                    raise SetupError(f"Register {provider} in subscription {target_sub} before setup")
                self._permissions(target_sub)
                found[key] = "Create a free-tier account only; paid fallback disabled"
                continue
            group, name = arm_parts(resource_id, provider, kind)
            if "search" in key:
                value = scoped_az("search", "service", "show", "-g", group, "-n", name)
                if value["sku"]["name"].lower() != "free":
                    raise SetupError("Only free-tier borrowed Search is supported for this exercise")
                if "aadOrApiKey" not in (value.get("authOptions") or {}):
                    raise SetupError("Borrowed Search must already permit Entra authentication; "
                                     "account-wide settings will not be changed")
                key_value = scoped_az("search", "admin-key", "show", "-g", group, "--service-name", name)["primaryKey"]
                request = urllib.request.Request("https://" + name + ".search.windows.net/indexes?api-version=2024-07-01&$select=name",
                                                 headers={"api-key": key_value})
                with urllib.request.urlopen(request, timeout=30) as response:
                    indexes = json.load(response)["value"]
                existing = any(index["name"] == self.config["search_index"] for index in indexes)
                owned = "search-index" in self.state.get("completed", {}) or "search-index" in self._resuming_pending
                if existing and not owned:
                    raise SetupError("Configured Search index already exists and is not owned by this deployment")
                if not existing and len(indexes) >= 3:
                    raise SetupError("Borrowed free Search already uses its three index slots")
            else:
                value = scoped_az("cosmosdb", "show", "-g", group, "-n", name)
                if value.get("kind") != "GlobalDocumentDB" or not value.get("enableFreeTier"):
                    raise SetupError("Borrowed Cosmos must be a free-tier NoSQL account")
                if len(value.get("locations", [])) != 1:
                    raise SetupError("Borrowed Cosmos must have one region; multi-region throughput can exceed the free allowance")
                databases = scoped_az("cosmosdb", "sql", "database", "list", "-g", group, "-a", name)
                total_ru = 0
                existing = False
                for database in databases:
                    database_name = database["name"]
                    existing |= database_name == self.config["cosmos_database"]
                    throughput = scoped_az("cosmosdb", "sql", "database", "throughput", "show", "-g", group,
                                    "-a", name, "-n", database_name, missing_ok=True)
                    if throughput:
                        resource = throughput["resource"]
                        total_ru += int(resource.get("throughput") or resource.get("autoscaleSettings", {}).get("maxThroughput", 0))
                    containers = scoped_az("cosmosdb", "sql", "container", "list", "-g", group, "-a", name, "-d", database_name)
                    for container in containers:
                        throughput = scoped_az("cosmosdb", "sql", "container", "throughput", "show", "-g", group,
                                        "-a", name, "-d", database_name, "-n", container["name"], missing_ok=True)
                        if throughput:
                            resource = throughput["resource"]
                            total_ru += int(resource.get("throughput") or resource.get("autoscaleSettings", {}).get("maxThroughput", 0))
                owned = ("cosmos-database" in self.state.get("completed", {}) or "cosmos-database" in self._resuming_pending)
                if existing and not owned:
                    raise SetupError("Configured Cosmos database already exists without deployment ownership")
                if total_ru + (0 if existing else 400) > 1000:
                    raise SetupError("Borrowed Cosmos has insufficient unused free-tier RU capacity")
            found[key] = resource_id
        return found

    def _entra_preflight(self):
        user = az("ad", "signed-in-user", "show")
        self.config.setdefault("writer_object_id", "")
        self.config["writer_object_id"] = self.config["writer_object_id"] or user["id"]
        self.config["sql_admin_object_id"] = self.config.get("sql_admin_object_id") or user["id"]
        self.config["sql_admin_name"] = self.config.get("sql_admin_name") or user["userPrincipalName"]
        for service in ("generation", "ingestion", "gateway"):
            name = "id-medw-" + service
            principals = az("ad", "sp", "list", "--filter", "displayName eq '" + name + "'")
            recorded = {identity["principalId"] for identity in self.state.get("identities", {}).values()}
            if any(principal["id"] not in recorded for principal in principals):
                raise SetupError(f"Existing principal {name} would make historical SQL identity lookup ambiguous; "
                                 "reuse the original journal or remove the obsolete deployment first")
        # A successful read is not proof of write permission. Record that app
        # registration is attempted before any billable infrastructure is made.
        az("ad", "app", "list", "--display-name", self.config["prefix"] + "-api")
        return {"writer_object_id": self.config["writer_object_id"],
                "sql_admin_object_id": self.config["sql_admin_object_id"],
                "app_registration": "created before paid infrastructure; tenant policy may deny"}

    def _git_preflight(self):
        url = self.config["git_url"]
        if not re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url):
            raise SetupError("Current pipeline commissioning requires a GitHub HTTPS git_url")
        branch = run(["git", "ls-remote", url, "refs/heads/" + self.config["git_branch"]])
        if not branch.strip():
            raise SetupError("Configured Git branch does not exist on the remote")
        return "Configured source branch is reachable"

    def _devops_preflight(self):
        cfg = self.config["devops"]
        self.devops("projects/" + urllib.parse.quote(cfg["project"], safe="") + "?api-version=7.1",
                    project=False)
        if not cfg.get("github_service_connection_id"):
            ready = [endpoint for endpoint in self.devops("serviceendpoint/endpoints?api-version=7.1")["value"]
                     if endpoint.get("type", "").lower() == "github" and endpoint.get("isReady")]
            if len(ready) == 1:
                cfg["github_service_connection_id"] = ready[0]["id"]
        if not cfg.get("github_service_connection_id"):
            raise SetupError("Set devops.github_service_connection_id to the GitHub connection "
                             "authorized to read source and push release-selection commits")
        endpoint = self.devops("serviceendpoint/endpoints/" + cfg["github_service_connection_id"]
                               + "?api-version=7.1")
        if endpoint.get("type", "").lower() != "github" or not endpoint.get("isReady"):
            raise SetupError("Configured GitHub service connection is not ready")
        pools = self.devops("distributedtask/pools?api-version=7.1", project=False)["value"]
        if cfg.get("agent_pool"):
            matching = [pool for pool in pools if pool["name"] == cfg["agent_pool"]]
            if not matching:
                raise SetupError("Configured self-hosted agent pool does not exist")
            agents = self.devops(f"distributedtask/pools/{matching[0]['id']}/agents?"
                                 "includeCapabilities=true&api-version=7.1", project=False)["value"]
            if not any(agent.get("enabled") and agent.get("status") == "online" for agent in agents):
                raise SetupError("Configured agent pool has no enabled online agent")
        proof = self.state.get("capacity_probe", {})
        timestamp = dt.datetime.fromisoformat(proof["timestamp"]) if proof.get("timestamp") else None
        if (not proof.get("passed") or proof.get("agent_pool") != cfg.get("agent_pool", "")
                or proof.get("probe_sha256") != hashlib.sha256((self.root / "deploy/azure-pipelines/preflight.yml").read_bytes()).hexdigest()
                or timestamp is None or (dt.datetime.now(dt.UTC) - timestamp).total_seconds() > 86400):
            print("Verifying build capacity and GitHub write access with a temporary pipeline; "
                  "no Azure infrastructure is created.", flush=True)
            self.probe_build()
        return "DevOps project, source connection and configured build capacity available"

    def probe_build(self):
        """Run a no-Azure build on an isolated branch; clean branch/definition."""
        self.account()
        cfg = self.config["devops"]
        endpoints = self.devops("serviceendpoint/endpoints?api-version=7.1")["value"]
        ready = [item for item in endpoints if item.get("type", "").lower() == "github"
                 and item.get("isReady") and (not cfg.get("github_service_connection_id")
                      or item["id"] == cfg["github_service_connection_id"])]
        if len(ready) != 1:
            raise SetupError("Configure one ready GitHub service connection before the capacity probe")
        cfg["github_service_connection_id"] = ready[0]["id"]
        branch = "medw-preflight-" + self.config["owner"] + "-" + uuid.uuid4().hex[:8]
        definition_id = None
        pushed = False
        result = {"passed": False, "agent_pool": cfg.get("agent_pool", ""),
                  "timestamp": dt.datetime.now(dt.UTC).isoformat(),
                  "probe_sha256": hashlib.sha256((self.root / "deploy/azure-pipelines/preflight.yml").read_bytes()).hexdigest()}
        with tempfile.TemporaryDirectory(prefix="medw-capacity-") as temporary:
            checkout = pathlib.Path(temporary) / "source"
            remote = run(["git", "remote", "get-url", "origin"]).strip()
            run(["git", "clone", "--quiet", "--depth", "1", "--branch", self.config["git_branch"],
                 remote, str(checkout)])
            try:
                run(["git", "-C", str(checkout), "checkout", "-b", branch])
                path = checkout / "deploy/azure-pipelines/preflight.yml"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((self.root / "deploy/azure-pipelines/preflight.yml").read_text())
                run(["git", "-C", str(checkout), "add", "deploy/azure-pipelines/preflight.yml"])
                for field in ("name", "email"):
                    value = run(["git", "config", "user." + field]).strip()
                    run(["git", "-C", str(checkout), "config", "user." + field, value])
                if run(["git", "-C", str(checkout), "diff", "--cached", "--name-only"]).strip():
                    run(["git", "-C", str(checkout), "commit", "-m", "Verify Azure Pipelines build capacity"])
                run(["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/" + branch])
                pushed = True
                queues = az("pipelines", "queue", "list", "--organization", cfg["organization"],
                            "--project", cfg["project"])
                queue_name = cfg.get("agent_pool") or "Azure Pipelines"
                queue = next((item for item in queues if item["name"] == queue_name), None)
                if queue is None:
                    raise SetupError("Configured build pool is not shared with this project")
                repo = urllib.parse.urlsplit(self.config["git_url"]).path.strip("/").removesuffix(".git")
                definition = self.devops("build/definitions?api-version=7.1", method="POST", body={
                    "name": branch, "type": "build", "queue": {"id": queue["id"]},
                    "process": {"type": 2, "yamlFilename": "deploy/azure-pipelines/preflight.yml"},
                    "repository": {"id": repo, "name": repo, "type": "GitHub", "url": self.config["git_url"],
                        "defaultBranch": "refs/heads/" + branch, "properties": {"connectedServiceId": ready[0]["id"]}}})
                definition_id = definition["id"]
                self.devops("pipelines/pipelinepermissions/endpoint/" + ready[0]["id"] + "?api-version=7.1-preview.1",
                            method="PATCH", body={"pipelines": [{"id": definition_id, "authorized": True}]})
                queued = self.devops(f"pipelines/{definition_id}/runs?api-version=7.1", method="POST", body={
                    "resources": {"repositories": {"self": {"refName": "refs/heads/" + branch}}},
                    "templateParameters": ({"agentPool": cfg["agent_pool"]} if cfg.get("agent_pool") else {})})
                result.update(run_id=queued["id"], pipeline_id=definition_id, url=queued.get("url"))
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    status = self.devops(f"pipelines/{definition_id}/runs/{queued['id']}?api-version=7.1")
                    if status.get("state") == "completed":
                        result["passed"] = status.get("result") == "succeeded"
                        result["result"] = status.get("result")
                        result["run"] = status
                        break
                    print("Waiting for non-Azure build-capacity probe", queued["id"], flush=True)
                    time.sleep(15)
                if not result["passed"]:
                    raise SetupError("Hosted build probe did not succeed; inspect the run and enable "
                                     "free hosted capacity or configure an online local agent")
                return result
            finally:
                self.state["capacity_probe"] = result
                self.save()
                try:
                    if result.get("run_id") and not result.get("result"):
                        self.devops("build/builds/" + str(result["run_id"]) + "?api-version=7.1",
                                    method="PATCH", body={"status": "cancelling"})
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            status = self.devops("build/builds/" + str(result["run_id"]) + "?api-version=7.1")
                            if status.get("status") == "completed":
                                break
                            time.sleep(5)
                    if definition_id is not None:
                        self.devops("build/definitions/" + str(definition_id) + "?api-version=7.1", method="DELETE")
                finally:
                    if pushed:
                        run(["git", "-C", str(checkout), "push", "origin", "--delete", branch])

    def _cost(self):
        path = self.directory / "retail-prices.json"
        source = "https://prices.azure.com/api/retail/prices"
        try:
            cache = json.loads(path.read_text()) if path.exists() else {}
            fetched_at = dt.datetime.fromisoformat(cache["fetched_at"]) if cache.get("fetched_at") else None
            age = (dt.datetime.now(dt.UTC) - fetched_at).total_seconds() if fetched_at else -1
            valid_quotes = all(math.isfinite(float(cache["quotes"][key]["hourly_aud"]))
                               and float(cache["quotes"][key]["hourly_aud"]) > 0
                               for key in ("node", "registry", "sql", "disk", "load_balancer", "public_ip"))
        except (KeyError, TypeError, ValueError):
            cache, age, valid_quotes = {}, -1, False
        if (cache.get("location") != self.config["location"] or cache.get("currency") != "AUD"
                or cache.get("source") != source or not valid_quotes or not 0 <= age <= 86400):
            quotes = retail_prices(self.config["location"])
            if not all(math.isfinite(row["hourly_aud"]) and row["hourly_aud"] > 0 for row in quotes.values()):
                raise SetupError("Retail price response contains an invalid/nonpositive hourly quote")
            cache = {"location": self.config["location"], "currency": "AUD", "source": source,
                     "fetched_at": dt.datetime.now(dt.UTC).isoformat(), "quotes": quotes}
            write_private(path, json.dumps(cache, indent=2))
            age = 0
        cost = estimate_cost(cache["quotes"], self.config["hours"])
        cost.update(quoted_at=cache["fetched_at"], quote_age_seconds=round(age), price_source=source)
        if cost["estimated_aud"] > self.config["budget_aud"]:
            raise SetupError(f"Estimated AUD {cost['estimated_aud']} exceeds configured "
                             f"AUD {self.config['budget_aud']}; shorten the exercise")
        return cost

    def checkpoint(self, name: str, action):
        if name in self.state.get("completed", {}):
            return self.state["completed"][name]
        self.state.setdefault("pending", {})[name] = dt.datetime.now(dt.UTC).isoformat()
        self.save()
        result = action()
        self.state.setdefault("completed", {})[name] = result
        self.state["pending"].pop(name, None)
        self.save()
        return result

    def _owned_group(self):
        config = self.config
        existing = az("group", "show", "-n", config["resource_group"], missing_ok=True)
        if existing and existing.get("tags", {}).get("medw-owner") != config["owner"]:
            raise SetupError("Refusing to adopt an existing resource group without the ownership tag")
        if not existing:
            existing = az("group", "create", "-n", config["resource_group"], "-l", config["location"],
                          "--tags", "medw-owner=" + config["owner"], "application=medwriter-assist")
        self.state["resource_group_id"] = existing["id"]
        self.state["owner"] = config["owner"]
        self.save()
        return existing

    def _identity(self, service: str) -> dict:
        name = "id-medw-" + ("ingestion" if service == "ingestion-worker" else service)
        group = self.config["resource_group"]
        identity = az("identity", "show", "-g", group, "-n", name, missing_ok=True)
        if identity is None:
            identity = az("identity", "create", "-g", group, "-n", name,
                          "--tags", "medw-owner=" + self.config["owner"])
        return identity

    def _role(self, principal: str, role: str, scope: str):
        # Deterministic names make retries after CLI/network errors idempotent.
        name = str(uuid.uuid5(uuid.NAMESPACE_URL, principal + role + scope.lower()))
        self.state.setdefault("role_assignments", {})[name] = (
            scope + "/providers/Microsoft.Authorization/roleAssignments/" + name)
        self.save()
        result = az("role", "assignment", "create", "--name", name,
                    "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                    "--role", role, "--scope", scope, subscription=scope.split("/")[2])
        self.state.setdefault("role_assignments", {})[name] = result["id"]
        self.save()
        return result

    def _cosmos_owned_operation(self, key: str, resource_id: str, action):
        # Persist deterministic ownership before ARM accepts the request. A lost
        # create response must not leave privileges behind in a borrowed account.
        self.state.setdefault("planned_cosmos_roles", {})[key] = {"id": resource_id}
        self.save()
        return self.checkpoint(key, action)

    def _applications(self):
        """Create owned API + public device-code client, with no client secret."""
        cfg = self.config
        apps = {}
        for suffix in ("api", "client"):
            name = cfg["prefix"] + "-" + suffix
            matches = az("ad", "app", "list", "--display-name", name)
            known = self.state.get("applications", {}).get(suffix)
            if matches and (not known or known["id"] != matches[0]["id"]):
                raise SetupError(f"Application {name} already exists without an ownership record")
            app = matches[0] if matches else az("ad", "app", "create", "--display-name", name,
                                               "--sign-in-audience", "AzureADMyOrg")
            apps[suffix] = {"id": app["id"], "appId": app["appId"]}
            self.state.setdefault("applications", {})[suffix] = apps[suffix]
            self.save()
        scope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, apps["api"]["appId"] + "/access"))
        api_uri = "api://" + apps["api"]["appId"]
        api_body: dict[str, Any] = {"identifierUris": [api_uri], "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [{"id": scope_id, "value": "access", "type": "User",
                "isEnabled": True, "adminConsentDisplayName": "Access medical writer API",
                "adminConsentDescription": "Use the API subject to study membership",
                "userConsentDisplayName": "Access medical writer API",
                "userConsentDescription": "Use the API subject to study membership"}],
            "preAuthorizedApplications": [{"appId": apps["client"]["appId"],
                                            "delegatedPermissionIds": [scope_id]}]}}
        client_body = {"isFallbackPublicClient": True, "publicClient": {
            "redirectUris": ["http://localhost"]}, "requiredResourceAccess": [{
                "resourceAppId": apps["api"]["appId"],
                "resourceAccess": [{"id": scope_id, "type": "Scope"}]}]}
        preauthorized = api_body["api"].pop("preAuthorizedApplications")
        for suffix, body in (("api", api_body), ("client", client_body)):
            az("rest", "--method", "PATCH", "--url",
               "https://graph.microsoft.com/v1.0/applications/" + apps[suffix]["id"],
               "--body", json.dumps(body))
            principals = az("ad", "sp", "list", "--filter",
                            "appId eq '" + apps[suffix]["appId"] + "'")
            if not principals:
                az("ad", "sp", "create", "--id", apps[suffix]["appId"])
        # Graph validates preauthorization against already-persisted scopes;
        # submitting a new scope and its preauthorization in one PATCH fails.
        az("rest", "--method", "PATCH", "--url",
           "https://graph.microsoft.com/v1.0/applications/" + apps["api"]["id"],
           "--body", json.dumps({"api": {"preAuthorizedApplications": preauthorized}}))
        return apps

    def _account_location(self, service: str) -> tuple[str, str]:
        """Optional free-account creation in another subscription; never borrow its RG."""
        subscription = self.config.get(service + "_subscription_id") or self.config["subscription_id"]
        if subscription == self.config["subscription_id"]:
            return subscription, self.config["resource_group"]
        group_name = self.config["resource_group"] + "-" + service
        group = az("group", "show", "-n", group_name, subscription=subscription, missing_ok=True)
        if group and group.get("tags", {}).get("medw-owner") != self.config["owner"]:
            raise SetupError("Refusing to adopt an existing cross-subscription resource group")
        if not group:
            group = az("group", "create", "-n", group_name, "-l", self.config["location"],
                       "--tags", "medw-owner=" + self.config["owner"], subscription=subscription)
        self.state.setdefault("extra_owned_groups", {})[group["id"]] = {
            "subscription": subscription, "name": group_name}
        self.save()
        return subscription, group_name

    def _resources(self):
        c = self.config
        p, group, location = c["prefix"], c["resource_group"], c["location"]
        self._owned_group()
        storage = self.checkpoint("storage", lambda: az("storage", "account", "create", "-n", p + "sa",
            "-g", group, "-l", location, "--sku", "Standard_LRS", "--kind", "StorageV2",
            "--allow-blob-public-access", "false", "--min-tls-version", "TLS1_2"))
        storage_id = storage["id"]
        # The signed-in operator must create containers and later verify uploads.
        user = az("ad", "signed-in-user", "show")
        self.checkpoint("operator-storage-role", lambda: az("role", "assignment", "create",
            "--name", str(uuid.uuid5(uuid.NAMESPACE_URL, storage_id + user["id"])),
            "--assignee-object-id", user["id"], "--assignee-principal-type", "User",
            "--role", "Storage Blob Data Contributor", "--scope", storage_id))
        for container in ("raw", "parsed", "snapshots"):
            self.checkpoint("container-" + container, lambda container=container:
                retry_rbac(lambda: az("storage", "container", "create", "--account-name", p + "sa", "-n", container,
                   "--auth-mode", "login")))
        if c.get("borrowed_search_id"):
            search_group, search_name = arm_parts(c["borrowed_search_id"],
                                                  "Microsoft.Search", "searchServices")
            search = az("search", "service", "show", "-g", search_group, "-n", search_name,
                        subscription=c["borrowed_search_id"].split("/")[2])
        else:
            search_subscription, search_group = self._account_location("search")
            search = self.checkpoint("search", lambda: az("search", "service", "create", "-g", search_group,
                "-n", p + "search", "-l", location, "--sku", "free", "--auth-options", "aadOrApiKey", "--aad-auth-failure-mode",
                "http401WithBearerChallenge", subscription=search_subscription))
        if c.get("borrowed_cosmos_id"):
            cosmos_group, cosmos_name = arm_parts(c["borrowed_cosmos_id"],
                                                  "Microsoft.DocumentDB", "databaseAccounts")
            cosmos = az("cosmosdb", "show", "-g", cosmos_group, "-n", cosmos_name,
                        subscription=c["borrowed_cosmos_id"].split("/")[2])
        else:
            cosmos_subscription, cosmos_group = self._account_location("cosmos")
            cosmos = self.checkpoint("cosmos", lambda: az("cosmosdb", "create", "-g", cosmos_group,
                "-n", p + "cosmos", "--locations", "regionName=" + location,
                "--enable-free-tier", "true", "--default-consistency-level", "Session", subscription=cosmos_subscription))
        cosmos_group, cosmos_name = arm_parts(cosmos["id"], "Microsoft.DocumentDB", "databaseAccounts")
        cosmos_az = partial(az, subscription=cosmos["id"].split("/")[2])
        database_name = c["cosmos_database"]
        database = cosmos_az("cosmosdb", "sql", "database", "show", "-g", cosmos_group, "-a", cosmos_name,
                      "-n", database_name, missing_ok=True)
        if (database and "cosmos-database" not in self.state.get("completed", {})
                and "cosmos-database" not in self.state.get("pending", {})):
            raise SetupError("Existing Cosmos database has no ownership journal; choose a new name")
        if not database:
            self.state.setdefault("claimed", {})["cosmos-database"] = {
                "account": cosmos["id"], "name": database_name}
            self.save()
        self.checkpoint("cosmos-database", lambda: cosmos_az("cosmosdb", "sql", "database", "create",
            "-g", cosmos_group, "-a", cosmos_name, "-n", database_name, "--throughput", "400"))
        for container, partition in (("documents", "/study_id"), ("sessions", "/user_id"),
                                     ("platform-state", "/study_id")):
            self.checkpoint("cosmos-container-" + container, lambda container=container, partition=partition:
                cosmos_az("cosmosdb", "sql", "container", "create", "-g", cosmos_group, "-a", cosmos_name,
                   "-d", database_name, "-n", container, "--partition-key-path", partition,
                   *(["--ttl", "43200"] if container == "sessions" else [])))
        sql = self.checkpoint("sql-server", lambda: az("sql", "server", "create", "-g", group,
            "-n", p + "sql", "-l", location, "--enable-ad-only-auth",
            "--external-admin-principal-type", c.get("sql_admin_type", "User"),
            "--external-admin-name", c["sql_admin_name"],
            "--external-admin-sid", c["sql_admin_object_id"]))
        self.checkpoint("sql-proxy", lambda: az("sql", "server", "conn-policy", "update", "-g", group,
            "-s", p + "sql", "--connection-type", "Proxy"))
        self.checkpoint("sql-database", lambda: az("sql", "db", "create", "-g", group,
            "-s", p + "sql", "-n", c["sql_database"], "--service-objective", "Basic"))
        self.checkpoint("sql-azure-firewall", lambda: az("sql", "server", "firewall-rule", "create",
            "-g", group, "-s", p + "sql", "-n", "allow-azure", "--start-ip-address", "0.0.0.0",
            "--end-ip-address", "0.0.0.0"))
        registry = self.checkpoint("registry", lambda: az("acr", "create", "-g", group, "-n", p + "acr",
            "--sku", "Basic", "--admin-enabled", "false"))
        logs = self.checkpoint("logs", lambda: az("monitor", "log-analytics", "workspace", "create",
            "-g", group, "-n", p + "logs", "-l", location, "--retention-time", "30"))
        insights_id = self.state["resource_group_id"] + "/providers/Microsoft.Insights/components/" + p + "ai"
        self.checkpoint("insights", lambda: az("rest", "--method", "PUT", "--url",
            API + insights_id + "?api-version=2020-02-02", "--body", json.dumps({
                "location": location, "kind": "web", "properties": {"Application_Type": "web",
                    "WorkspaceResourceId": logs["id"], "IngestionMode": "LogAnalytics"}})))
        cluster = self.checkpoint("aks", lambda: az("aks", "create", "-g", group, "-n", p + "aks",
            "--tier", "free", "--node-count", "1", "--node-vm-size", "Standard_D4s_v5",
            "--node-osdisk-size", "64", "--network-plugin", "azure", "--network-plugin-mode", "overlay",
            "--network-dataplane", "cilium", "--pod-cidr", "192.168.0.0/16",
            "--service-cidr", "10.0.0.0/16", "--dns-service-ip", "10.0.0.10",
            "--attach-acr", registry["id"], "--enable-oidc-issuer", "--enable-workload-identity",
            "--enable-managed-identity", "--generate-ssh-keys"))
        result = {"storage": storage_id, "blob_url": storage["primaryEndpoints"]["blob"].rstrip("/"),
                  "search": search["id"], "search_url": "https://" + search["name"] + ".search.windows.net",
                  "cosmos": cosmos["id"], "cosmos_url": cosmos["documentEndpoint"],
                  "sql": sql["id"], "sql_server": sql["fullyQualifiedDomainName"],
                  "registry": registry["id"], "registry_host": registry["loginServer"],
                  "cluster": cluster["id"], "oidc_issuer": cluster["oidcIssuerProfile"]["issuerUrl"],
                  "insights": insights_id}
        self.state["resources"] = result
        self.save()
        return result

    def _workload_access(self):
        c, resources = self.config, self.state["resources"]
        identities = {service: self.checkpoint("identity-" + service,
                      lambda service=service: self._identity(service))
                      for service in (*SERVICES, "qdrant-backup", "delivery")}
        self.state["identities"] = identities
        self.save()
        cosmos_group, cosmos_name = arm_parts(resources["cosmos"],
                                              "Microsoft.DocumentDB", "databaseAccounts")
        cosmos_az = partial(az, subscription=resources["cosmos"].split("/")[2])
        access = {
            "gateway": [("Storage Blob Delegator", resources["storage"]),
                        ("Storage Blob Data Contributor", resources["storage"] + "/blobServices/default/containers/raw")],
            "retrieval": [("Search Index Data Reader", resources["search"])],
            "generation": [("Storage Blob Data Reader", resources["storage"] + "/blobServices/default/containers/raw")],
            "ingestion-worker": [("Search Index Data Contributor", resources["search"]),
                ("Storage Blob Data Contributor", resources["storage"] + "/blobServices/default/containers/raw"),
                ("Storage Blob Data Contributor", resources["storage"] + "/blobServices/default/containers/parsed")],
            "qdrant-backup": [("Storage Blob Data Contributor", resources["storage"] + "/blobServices/default/containers/snapshots")],
            "delivery": [("AcrPush", resources["registry"])], "reranker": [],
        }
        cosmos_access = {"gateway": [("writer", "documents"), ("writer", "sessions")],
                         "retrieval": [("reader", "platform-state")],
                         "generation": [("evidence-appender", "platform-state")],
                         "ingestion-worker": [("platform-writer", "platform-state"), ("writer", "documents")]}
        role_ids = {"reader": resources["cosmos"] + "/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001",
                    "writer": resources["cosmos"] + "/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"}
        for role in ("evidence-appender", "platform-writer"):
            body = json.loads((ROOT / "infra/cosmos" / (role + ".json")).read_text())
            body["Id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, resources["cosmos"] + c["cosmos_database"] + role))
            body["RoleName"] = c["cosmos_database"] + "-" + role
            body["AssignableScopes"] = ["/dbs/" + c["cosmos_database"] + "/colls/platform-state"]
            result = self._cosmos_owned_operation("cosmos-role-" + role,
                resources["cosmos"] + "/sqlRoleDefinitions/" + body["Id"], lambda body=body: cosmos_az("cosmosdb", "sql",
                "role", "definition", "create", "-g", cosmos_group, "-a", cosmos_name,
                "--body", json.dumps(body)))
            role_ids[role] = result["id"]
        for service, identity in identities.items():
            if service != "delivery":
                self.checkpoint("federation-" + service, lambda service=service, identity=identity:
                    az("identity", "federated-credential", "create", "-g", c["resource_group"],
                       "--identity-name", identity["name"], "--name", "fc-" + service,
                       "--issuer", resources["oidc_issuer"], "--subject", "system:serviceaccount:medw:" + service,
                       "--audiences", "api://AzureADTokenExchange"))
            for role, scope in access[service]:
                self.checkpoint("role-" + service + "-" + role + "-" + scope.rsplit("/", 1)[-1],
                    lambda role=role, scope=scope, identity=identity: self._role(identity["principalId"], role, scope))
            for role, container in cosmos_access.get(service, []):
                scope = resources["cosmos"] + "/dbs/" + c["cosmos_database"] + "/colls/" + container
                assignment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity["principalId"] + role + scope))
                self._cosmos_owned_operation("cosmos-assignment-" + service + "-" + container,
                    resources["cosmos"] + "/sqlRoleAssignments/" + assignment_id,
                    lambda identity=identity, role=role, scope=scope, assignment_id=assignment_id:
                    cosmos_az("cosmosdb", "sql", "role", "assignment", "create", "-g", cosmos_group,
                       "-a", cosmos_name, "--role-assignment-id", assignment_id,
                       "--principal-id", identity["principalId"], "--role-definition-id", role_ids[role],
                       "--scope", scope))

    def _sql_bootstrap(self):
        c, resources = self.config, self.state["resources"]
        source = run(["git", "rev-parse", "HEAD"]).strip()
        image = "medw-sql-bootstrap:" + source[:12]
        run(["docker", "build", "-f", "services/generation/Dockerfile", "--build-arg",
             "SOURCE_SHA=" + source, "-t", image, "."])
        # Only a short-lived operator-IP rule; never open SQL to the internet.
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as response:
            address = response.read().decode().strip()
        import ipaddress
        ipaddress.ip_address(address)
        server = c["prefix"] + "sql"
        rule = "medw-bootstrap-" + c["owner"]
        az("sql", "server", "firewall-rule", "create", "-g", c["resource_group"], "-s", server,
           "-n", rule, "--start-ip-address", address, "--end-ip-address", address)
        try:
            token = az("account", "get-access-token", "--resource", "https://database.windows.net/")["accessToken"]
            env = {"MEDW_SQL_ACCESS_TOKEN": token, "MEDW_SQL_SERVER": resources["sql_server"],
                   "MEDW_SQL_DATABASE": c["sql_database"],
                   "MEDW_AZURE_BOOTSTRAP": json.dumps({
                       "migration_object_id": self.state["identities"]["delivery"]["principalId"],
                       "study_id": c["study_id"], "section_path": c["section_path"],
                       "writer_object_id": c["writer_object_id"], "location": c["location"]})}
            run(["docker", "run", "--rm", *(item for name in env for item in ("--env", name)),
                 "--mount", f"type=bind,src={self.root},dst=/workspace,readonly", image,
                 "python", "/workspace/scripts/azure_sql.py"], env=env)
        finally:
            az("sql", "server", "firewall-rule", "delete", "-g", c["resource_group"], "-s", server,
               "-n", rule)
        return {"schema": "applied", "membership": c["study_id"], "migration_principal": "id-medw-delivery"}

    def kube(self, *args, payload=None, json_result=False):
        return run(["kubectl", "--kubeconfig", str(self.directory / "kubeconfig"), *args],
                   payload=payload, json_result=json_result)

    def apply(self, *objects):
        return self.kube("apply", "-f", "-", payload=yaml.safe_dump_all(objects))

    def _cluster_platform(self):
        c, resources = self.config, self.state["resources"]
        kubeconfig = self.directory / "kubeconfig"
        az("aks", "get-credentials", "-g", c["resource_group"], "-n", c["prefix"] + "aks",
           "--file", str(kubeconfig), "--overwrite-existing")
        os.chmod(kubeconfig, 0o600)
        kube_env = {"KUBECONFIG": str(kubeconfig)}
        run(["flux", "install", "--version", "v2.9.5"], env=kube_env)
        self.state["platform_versions"] = {"flux": "v2.9.5", "keda": "2.20.2",
                                           "kube_prometheus_stack": "90.0.0",
                                           "nginx_chart": "2.7.1", "nginx_controller": "5.6.1"}
        self.save()
        self.apply(*[{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
                     for name in ("medw", "monitoring", "keda")])
        repos = {"kedacore": "https://kedacore.github.io/charts",
                 "prometheus-community": "https://prometheus-community.github.io/helm-charts"}
        for name, url in repos.items():
            run(["helm", "repo", "add", name, url, "--force-update"])
        run(["helm", "repo", "update"])
        apply_options = helm_apply_options(run(["helm", "version", "--short"]))
        run(["helm", "upgrade", "--install", *apply_options, "keda", "kedacore/keda", "--namespace", "keda",
             "--version", "2.20.2", "--wait", "--timeout", "10m"], env=kube_env)
        prom_values = {"grafana": {"enabled": False}, "alertmanager": {"enabled": False},
                       "prometheus": {"prometheusSpec": {"retention": "6h",
                           "resources": {"requests": {"cpu": "100m", "memory": "512Mi"}},
                           "serviceMonitorSelectorNilUsesHelmValues": False,
                           "ruleSelectorNilUsesHelmValues": False}}}
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "prometheus.yaml"
            path.write_text(yaml.safe_dump(prom_values))
            run(["helm", "upgrade", "--install", *apply_options, "kps", "prometheus-community/kube-prometheus-stack",
                 "--namespace", "monitoring", "--version", "90.0.0", "-f", str(path),
                 "--wait", "--timeout", "10m"], env=kube_env)
        nginx = list(yaml.safe_load_all((ROOT / "deploy/nginx-ingress.yaml").read_text()))
        values = nginx[-1]["spec"]["values"]["controller"]
        values["replicaCount"] = 1
        values["service"]["annotations"] = {
            "service.beta.kubernetes.io/azure-dns-label-name": c["prefix"]}
        self.apply(*nginx)
        self.kube("-n", "nginx-ingress", "wait", "helmrelease/nginx-ingress",
                  "--for=condition=Ready", "--timeout=15m")
        host = c["prefix"] + "." + c["location"] + ".cloudapp.azure.com"
        self.state["hostname"] = host
        tls_dir = self.directory / "tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        key, certificate = tls_dir / "server.key", tls_dir / "server.crt"
        if not certificate.exists():
            run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "30",
                 "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=" + host,
                 "-addext", "subjectAltName=DNS:" + host])
            os.chmod(key, 0o600)
        self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "gateway-tls", "namespace": "medw"}, "type": "kubernetes.io/tls",
            "data": {"tls.crt": base64.b64encode(certificate.read_bytes()).decode(),
                     "tls.key": base64.b64encode(key.read_bytes()).decode()}})
        qdrant = self.kube("-n", "medw", "get", "secret", "qdrant-auth", "--ignore-not-found", "-o", "json")
        if not qdrant.strip():
            self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {
                "name": "qdrant-auth", "namespace": "medw"}, "type": "Opaque",
                "stringData": {"api-key": secrets.token_urlsafe(32),
                               "read-only-api-key": secrets.token_urlsafe(32)}})
        insights = az("rest", "--url", API + resources["insights"] + "?api-version=2020-02-02")
        self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "medw-telemetry", "namespace": "medw"}, "type": "Opaque",
            "stringData": {"connection-string": insights["properties"]["ConnectionString"]}})
        self.save()
        return {"hostname": host, "ca_file": str(certificate)}

    def environment_values(self) -> list[dict]:
        c, resources = self.config, self.state["resources"]
        documents = list(yaml.safe_load_all((self.root / "deploy/flux/dev/environment-values.yaml").read_text()))
        shared = {"backend": "azure", "cosmos_endpoint": resources["cosmos_url"],
                  "cosmos_database": c["cosmos_database"], "cosmos_state_container": "platform-state",
                  "blob_account_url": resources["blob_url"], "blob_container": "raw",
                  "sql_server": resources["sql_server"], "sql_database": c["sql_database"],
                  "search_endpoint": resources["search_url"], "search_index": c["search_index"],
                  "qdrant_url": "http://qdrant:6333", "qdrant_replication_factor": 1,
                  "qdrant_write_consistency_factor": 1, "reranker_url": "http://reranker:8000",
                  "retrieval_url": "http://retrieval:8000", "auth_tenant_id": c["tenant_id"],
                  "auth_audience": self.state["applications"]["api"]["appId"],
                  "auth_issuer": "https://login.microsoftonline.com/" + c["tenant_id"] + "/v2.0",
                  "auth_jwks_url": "https://login.microsoftonline.com/" + c["tenant_id"] + "/discovery/v2.0/keys"}
        for document in documents:
            name = document["metadata"]["name"]
            values = document["spec"].setdefault("values", {})
            if name == "qdrant":
                document["spec"]["suspend"] = False
                values.update(replicas=1, persistence={"storageClass": "managed-csi", "size": "8Gi"},
                    config={"replication_factor": 1, "write_consistency_factor": 1},
                    resources={"requests": {"cpu": "200m", "memory": "512Mi"},
                               "limits": {"cpu": "1", "memory": "1Gi"}},
                    podDisruptionBudget={"minAvailable": 0})
                snapshots = values.setdefault("snapshots", {})
                snapshots.update(enabled=True, accountUrl=resources["blob_url"], container="snapshots",
                    clientId=self.state["identities"]["qdrant-backup"]["clientId"])
                snapshots.setdefault("image", {})["repository"] = resources["registry_host"] + "/ingestion-worker"
                continue
            old = values.setdefault("config", {})
            # Existing env keys for unused remote providers must not accidentally
            # reinstate provisioning/readiness dependencies.
            for key in ("aoai_resource_id", "aoai_endpoint", "docintel_endpoint", "language_endpoint"):
                old.pop(key, None)
            old.update(shared)
            values.update(env="dev", replicas=1)
            values.setdefault("autoscaling", {}).update(minReplicas=1, maxReplicas=2)
            if name == "generation":
                values["autoscaling"].update(target=1, cooldownPeriod=30, stabilizationWindowSeconds=30)
            values.setdefault("image", {})["repository"] = resources["registry_host"] + "/" + name
            values.setdefault("serviceAccount", {}).setdefault("annotations", {})[
                "azure.workload.identity/client-id"] = self.state["identities"][name]["clientId"]
            if name == "gateway":
                values.setdefault("ingress", {}).update(host=self.state["hostname"], tls=True)
        return documents

    def _publish_configuration(self):
        if run(["git", "status", "--porcelain"]).strip():
            raise SetupError("Commit implementation changes before azure-up publishes deployment configuration")
        current = run(["git", "branch", "--show-current"]).strip()
        if current != self.config["git_branch"]:
            raise SetupError("Checkout the configured git_branch before azure-up")
        path = self.root / "deploy/flux/dev/environment-values.yaml"
        path.write_text(yaml.safe_dump_all(self.environment_values(), sort_keys=False))
        paths = [path]
        for relative in ("deploy/flux/base/source.yaml", "deploy/flux/dev/chart-source.yaml"):
            path = self.root / relative
            document = yaml.safe_load(path.read_text())
            document["spec"]["url"] = self.config["git_url"]
            if "branch" in document["spec"].get("ref", {}):
                document["spec"]["ref"]["branch"] = self.config["git_branch"]
            path.write_text(yaml.safe_dump(document, sort_keys=False))
            paths.append(path)
        # CI-triggered runs do not inherit parameters from the first manual run.
        # Persist infrastructure defaults so later source changes target this deployment.
        path = self.root / "deploy/azure-pipelines/delivery.yml"
        pipeline = yaml.safe_load(path.read_text())
        defaults = {"registryHost": self.state["resources"]["registry_host"],
                    "azureConnection": self.config["devops"]["azure_service_connection_name"],
                    "agentPool": self.config["devops"].get("agent_pool", ""),
                    "releaseBranch": self.config["git_branch"]}
        for parameter in pipeline["parameters"]:
            if parameter["name"] in defaults:
                parameter["default"] = defaults[parameter["name"]]
        pipeline["trigger"]["branches"]["include"] = [self.config["git_branch"]]
        path.write_text(yaml.safe_dump(pipeline, sort_keys=False))
        paths.append(path)
        run(["kubectl", "kustomize", "deploy/flux/dev"])
        run(["git", "add", *(str(path.relative_to(self.root)) for path in paths)])
        if run(["git", "diff", "--cached", "--name-only"]).strip():
            run(["git", "commit", "-m", "Configure Azure infrastructure and workload identities [skip ci]"])
            run(["git", "push", "origin", self.config["git_branch"]])
        return {"commit": run(["git", "rev-parse", "HEAD"]).strip()}

    def _pipeline(self):
        c, cfg = self.config, self.config["devops"]
        project = self.devops("projects/" + urllib.parse.quote(cfg["project"], safe="")
                             + "?api-version=7.1", project=False)
        identity = self.state["identities"]["delivery"]
        endpoint_name = cfg["azure_service_connection_name"]
        endpoints = self.devops("serviceendpoint/endpoints?api-version=7.1")["value"]
        found = [item for item in endpoints if item["name"] == endpoint_name]
        endpoint = self.state.get("service_connection")
        if found and (not endpoint or endpoint["id"] != found[0]["id"]):
            raise SetupError("Existing Azure service connection has no ownership record")
        if not found:
            body = {"name": endpoint_name, "type": "azurerm", "url": API + "/", "isReady": False,
                "authorization": {"scheme": "WorkloadIdentityFederation", "parameters": {
                    "tenantid": c["tenant_id"], "serviceprincipalid": identity["clientId"]}},
                "data": {"subscriptionId": c["subscription_id"], "subscriptionName": "medwriter Azure",
                         "environment": "AzureCloud", "scopeLevel": "Subscription", "creationMode": "Manual"},
                "serviceEndpointProjectReferences": [{"projectReference": {
                    "id": project["id"], "name": project["name"]}, "name": endpoint_name}]}
            endpoint = self.devops("serviceendpoint/endpoints?api-version=7.1", method="POST", body=body)
            self.state["service_connection"] = {"id": endpoint["id"], "name": endpoint_name}
            self.save()
        else:
            endpoint = found[0]
        params = endpoint.get("authorization", {}).get("parameters", {})
        data = endpoint.get("data", {})
        issuer = params.get("workloadIdentityFederationIssuer") or data.get("workloadIdentityFederationIssuer")
        subject = params.get("workloadIdentityFederationSubject") or data.get("workloadIdentityFederationSubject")
        if not issuer or not subject:
            raise SetupError("Azure DevOps did not return federation issuer/subject; connection stays unready")
        self.checkpoint("delivery-federation", lambda: az("identity", "federated-credential", "create",
            "-g", c["resource_group"], "--identity-name", identity["name"], "--name", "azure-pipelines",
            "--issuer", issuer, "--subject", subject, "--audiences", "api://AzureADTokenExchange"))
        endpoint["isReady"] = True
        self.devops("serviceendpoint/endpoints/" + endpoint["id"] + "?api-version=7.1",
                    method="PUT", body=endpoint)
        definitions = self.devops("build/definitions?api-version=7.1")["value"]
        found = [item for item in definitions if item["name"] == cfg["pipeline_name"]]
        if found:
            if self.state.get("pipeline_id") != found[0]["id"]:
                raise SetupError("Existing pipeline has no ownership record; choose another pipeline_name")
            pipeline_id = found[0]["id"]
        else:
            repo = urllib.parse.urlsplit(c["git_url"]).path.strip("/").removesuffix(".git")
            queue_list = az("pipelines", "queue", "list", "--organization", cfg["organization"],
                            "--project", cfg["project"])
            queue_name = cfg.get("agent_pool") or "Azure Pipelines"
            queue = next((item for item in queue_list if item["name"] == queue_name), None)
            if queue is None:
                raise SetupError("Selected build pool is not available to this DevOps project")
            body = {"name": cfg["pipeline_name"], "type": "build", "queue": {"id": queue["id"]},
                "process": {"type": 2, "yamlFilename": "deploy/azure-pipelines/delivery.yml"},
                "repository": {"id": repo, "name": repo, "type": "GitHub", "url": c["git_url"],
                    "defaultBranch": "refs/heads/" + c["git_branch"], "properties": {
                        "connectedServiceId": cfg["github_service_connection_id"]}},
                "variables": {"sqlServer": {"value": self.state["resources"]["sql_server"]},
                              "sqlDatabase": {"value": c["sql_database"]}}}
            created = self.devops("build/definitions?api-version=7.1", method="POST", body=body)
            pipeline_id = created["id"]
            self.state["pipeline_id"] = pipeline_id
            self.save()
        for resource_id in (endpoint["id"], cfg["github_service_connection_id"]):
            self.devops("pipelines/pipelinepermissions/endpoint/" + resource_id + "?api-version=7.1-preview.1",
                        method="PATCH", body={"pipelines": [{"id": pipeline_id, "authorized": True}]})
        return {"pipeline_id": pipeline_id}

    def queue_release(self) -> dict:
        c = self.config
        body = {"resources": {"repositories": {"self": {"refName": "refs/heads/" + c["git_branch"]}}},
                "templateParameters": {"registryHost": self.state["resources"]["registry_host"],
                    "azureConnection": c["devops"]["azure_service_connection_name"],
                    **({"agentPool": c["devops"]["agent_pool"]} if c["devops"].get("agent_pool") else {}),
                    "releaseBranch": c["git_branch"]}}
        result = self.devops(f"pipelines/{self.state['pipeline_id']}/runs?api-version=7.1",
                             method="POST", body=body)
        self.state.setdefault("runs", []).append({"id": result["id"], "url": result.get("url")})
        self.save()
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            status = self.devops(f"pipelines/{self.state['pipeline_id']}/runs/{result['id']}?api-version=7.1")
            if status.get("state") == "completed":
                if status.get("result") != "succeeded":
                    raise SetupError("Azure release pipeline failed; inspect run " + str(result["id"]))
                return {"id": result["id"], "result": status["result"]}
            print("Waiting for Azure release pipeline run", result["id"], flush=True)
            time.sleep(30)
        raise SetupError("Release pipeline did not complete within one hour")

    def up(self):
        if self.state.get("teardown_complete"):
            timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            write_private(self.directory / ("state-" + timestamp + ".json"), json.dumps(self.state, indent=2))
            self.state = {"capacity_probe": self.state.get("capacity_probe", {})}
            self._resuming_pending = set()
            self.save()
        report = self.preflight()
        write_private(self.directory / "preflight.json", json.dumps(report, indent=2))
        if not report["passed"]:
            failures = [row["name"] + ": " + row["detail"] for row in report["checks"] if not row["passed"]]
            raise SetupError("Preflight failed before paid creation:\n" + "\n".join(failures))
        if run(["git", "status", "--porcelain"]).strip():
            raise SetupError("Commit implementation changes before provisioning")
        self.state.setdefault("config", copy.deepcopy(self.config))
        self.state["owner"] = self.config["owner"]
        self.save()
        self.checkpoint("applications", self._applications)
        self._resources()
        self._workload_access()
        self.checkpoint("search-index", self._search_index)
        self.checkpoint("sql-bootstrap", self._sql_bootstrap)
        self._cluster_platform()
        self._publish_configuration()
        self._pipeline()
        self.queue_release()
        self.apply({"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
            "metadata": {"name": "medwriter-assist", "namespace": "flux-system"},
            "spec": {"interval": "1m", "url": self.config["git_url"],
                     "ref": {"branch": self.config["git_branch"]}}},
            {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
             "metadata": {"name": "medw-dev", "namespace": "flux-system"},
             "spec": {"interval": "1m", "path": "./deploy/flux/dev", "prune": True,
                      "wait": False, "sourceRef": {"kind": "GitRepository", "name": "medwriter-assist"}}})
        self.kube("-n", "flux-system", "wait", "kustomization/medw-dev",
                  "--for=condition=Ready", "--timeout=5m")
        self.kube("-n", "medw", "wait", "helmrelease", "--all", "--for=condition=Ready", "--timeout=15m")
        return {"url": "https://" + self.state["hostname"],
                "ca_file": str(self.directory / "tls/server.crt"), "pipeline": self.state["pipeline_id"]}

    def _search_index(self):
        from infra.search_payload import api_payload
        c, resources = self.config, self.state["resources"]
        group, name = arm_parts(resources["search"], "Microsoft.Search", "searchServices")
        key = az("search", "admin-key", "show", "-g", group, "--service-name", name,
                 subscription=resources["search"].split("/")[2])["primaryKey"]
        url = resources["search_url"] + "/indexes/" + c["search_index"] + "?api-version=2024-07-01"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"api-key": key}), timeout=30):
                if "search-index" not in self._resuming_pending:
                    raise SetupError("Search index already exists without an ownership record; choose a new name")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise SetupError(f"Search index inspection failed: HTTP{exc.code}") from exc
        self.state.setdefault("claimed", {})["search-index"] = {
            "account": resources["search"], "name": c["search_index"]}
        self.save()
        body = api_payload(json.loads((ROOT / "infra/search/csr-chunks-index.json").read_text()))
        body["name"] = c["search_index"]
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT",
                                         headers={"api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30):
            pass
        return {"name": c["search_index"], "account": resources["search"]}

    def verify(self):
        """Run the real acceptance exercise, preserve evidence, then clean up."""
        from scripts.azure_verify import verify
        from scripts.demo_run import acquire_token
        if not self.state.get("hostname"):
            raise SetupError("No deployed application; run azure-up first")
        result = None
        verification_error = None
        cleanup_error = None
        try:
            token = os.environ.get("MEDW_DEMO_TOKEN")
            provider = None if token else lambda: acquire_token(self.state, self.directory / "api-token-cache.json")
            result = verify(self, token=token, token_provider=provider)
        except (SetupError, OSError, ValueError, RuntimeError, AssertionError, KeyError) as exc:
            verification_error = evidence_error(exc)
        finally:
            # The collector writes each result before teardown. Cleanup failures
            # must not hide failed checks or claim that spending has stopped.
            try:
                cleanup = self.down()
            except (SetupError, OSError, ValueError, KeyError) as exc:
                cleanup = {"complete": False, "error": evidence_error(exc)}
                cleanup_error = evidence_error(exc)
            summary = {"passed": bool(result and result.get("passed")),
                       "evidence": str(self.directory / "acceptance.json"), "cleanup": cleanup}
            if verification_error:
                summary["error"] = verification_error
            write_private(self.directory / "verification.json", json.dumps(summary, indent=2))
        if verification_error or cleanup_error or not summary["passed"]:
            raise SetupError("Azure verification or cleanup incomplete; inspect "
                             + str(self.directory / "verification.json"))
        return summary

    def down(self):
        """Delete only journalled owned infrastructure and scoped borrowed data."""
        if not self.journal_path.exists():
            raise SetupError("No ownership journal; refusing resource deletion")
        self.account()
        if self.state.get("owner") != self.config["owner"]:
            raise SetupError("Ownership journal does not match the subscription/resource group")
        original = self.state.get("config", {})
        for key in ("subscription_id", "resource_group", "borrowed_search_id", "borrowed_cosmos_id",
                    "cosmos_database", "search_index", "search_subscription_id", "cosmos_subscription_id"):
            if original.get(key) != self.config.get(key):
                raise SetupError(f"Cleanup configuration differs from recorded {key}")
        errors = []

        def attempt(label, action):
            if label in self.state.get("deleted", []):
                return
            try:
                action()
                self.state.setdefault("deleted", []).append(label)
                self.save()
            except (SetupError, OSError, ValueError, KeyError) as exc:
                errors.append({"resource": label, "error": evidence_error(exc)})

        completed = self.state.get("completed", {})
        if self.config.get("borrowed_search_id") and ("search-index" in completed
                or "search-index" in self.state.get("claimed", {})):
            def delete_index():
                group, name = arm_parts(self.config["borrowed_search_id"], "Microsoft.Search", "searchServices")
                key = az("search", "admin-key", "show", "-g", group, "--service-name", name,
                         subscription=self.config["borrowed_search_id"].split("/")[2])["primaryKey"]
                url = "https://" + name + ".search.windows.net/indexes/" + self.config["search_index"] + "?api-version=2024-07-01"
                request = urllib.request.Request(url, headers={"api-key": key}, method="DELETE")
                try:
                    with urllib.request.urlopen(request, timeout=30):
                        pass
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise SetupError("Search cleanup failed") from exc
            attempt("borrowed-search-index", delete_index)
        if self.config.get("borrowed_cosmos_id"):
            group, name = arm_parts(self.config["borrowed_cosmos_id"], "Microsoft.DocumentDB", "databaseAccounts")
            cosmos_az = partial(az, subscription=self.config["borrowed_cosmos_id"].split("/")[2])
            cosmos_roles = {**self.state.get("planned_cosmos_roles", {}), **completed}
            for key, value in sorted(cosmos_roles.items(), key=lambda item: (
                    0 if item[0].startswith("cosmos-assignment-") else 1, item[0])):
                if key.startswith("cosmos-assignment-"):
                    attempt(key, lambda value=value: cosmos_az("cosmosdb", "sql", "role", "assignment", "delete",
                        "-g", group, "-a", name, "--role-assignment-id", value["id"].rsplit("/", 1)[-1],
                        missing_ok=True))
                if key.startswith("cosmos-role-"):
                    attempt(key, lambda value=value: cosmos_az("cosmosdb", "sql", "role", "definition", "delete",
                        "-g", group, "-a", name, "--role-definition-id", value["id"].rsplit("/", 1)[-1],
                        missing_ok=True))
            if "cosmos-database" in completed or "cosmos-database" in self.state.get("claimed", {}):
                attempt("borrowed-cosmos-database", lambda: cosmos_az("cosmosdb", "sql", "database", "delete",
                    "-g", group, "-a", name, "-n", self.config["cosmos_database"], "--yes"))
        for name, resource_id in self.state.get("role_assignments", {}).items():
            attempt("role-" + name, lambda resource_id=resource_id: az("role", "assignment", "delete", "--ids", resource_id,
                    subscription=resource_id.split("/")[2]))
        if self.state.get("pipeline_id"):
            attempt("pipeline", lambda: self.devops("build/definitions/" + str(self.state["pipeline_id"])
                    + "?api-version=7.1", method="DELETE"))
        if self.state.get("service_connection"):
            attempt("azure-service-connection", lambda: self.devops("serviceendpoint/endpoints/"
                    + self.state["service_connection"]["id"] + "?api-version=7.1", method="DELETE"))
        for kind, app in self.state.get("applications", {}).items():
            attempt("application-" + kind, lambda app=app: az("ad", "app", "delete", "--id", app["id"]))
        group = az("group", "show", "-n", self.config["resource_group"], missing_ok=True)
        if group:
            if group.get("tags", {}).get("medw-owner") != self.config["owner"]:
                raise SetupError("Ownership tag missing/changed; refusing resource-group deletion")
            attempt("owned-resource-group", lambda: az("group", "delete", "-n", self.config["resource_group"], "--yes"))
        for resource_id, item in self.state.get("extra_owned_groups", {}).items():
            group = az("group", "show", "-n", item["name"], subscription=item["subscription"], missing_ok=True)
            if group:
                if group.get("tags", {}).get("medw-owner") != self.config["owner"]:
                    errors.append({"resource": resource_id, "error": "Ownership tag changed; preserved"})
                else:
                    attempt(resource_id, lambda item=item: az("group", "delete", "-n", item["name"], "--yes",
                            subscription=item["subscription"]))
        result = {"remaining_or_failed": errors, "complete": not errors,
                  "borrowed_accounts_preserved": True}
        write_private(self.directory / "cleanup.json", json.dumps(result, indent=2))
        if errors:
            raise SetupError("Cleanup incomplete; inspect " + str(self.directory / "cleanup.json"))
        self.state["teardown_complete"] = True
        self.save()
        for filename in ("api-token-cache.json", "api-token.txt"):
            (self.directory / filename).unlink(missing_ok=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "probe-build", "up", "verify", "down"))
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "data/azure/config.json")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    args.config = args.config.resolve()
    if args.output:
        args.output = args.output.resolve()
    os.chdir(ROOT)
    try:
        deployment = Deployment(load_config(args.config))
        # Prevent two operators from provisioning/deleting the same journal.
        import fcntl
        deployment.directory.mkdir(parents=True, exist_ok=True)
        with (deployment.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = getattr(deployment, args.action.replace("-", "_"))()
        serialized = json.dumps(result, indent=2)
        if args.output:
            write_private(args.output, serialized + "\n")
        print(serialized)
        if args.action == "preflight" and not result["passed"]:
            return 2
    except (SetupError, FileNotFoundError, BlockingIOError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
