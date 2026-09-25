#!/usr/bin/env python3
"""Read-only checks Terraform cannot establish: free capacity, quota and prices.

This script never creates resources, chooses a paid fallback or drives apply.
Terraform configuration/state, rather than a separate deployment journal, owns
resource identity. Azure and Terraform permission errors are fatal.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime


def az(*arguments, subscription=None, missing_offer=False):
    command = ["az", *arguments, "--only-show-errors", "-o", "json"]
    if subscription:
        command += ["--subscription", subscription]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        if missing_offer and any(code in result.stderr for code in
                                 ("(NotFound)", "(ResourceNotFound)", "(OfferNotFound)")):
            return None
        raise RuntimeError(f"Azure read failed: {arguments[0]} {arguments[1]}; check access/configuration")
    return json.loads(result.stdout) if result.stdout.strip() else None


def http_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


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
            raise RuntimeError(f"No unambiguous AUD retail quote available for {kind}")
        row = min(candidates, key=lambda item: item["retailPrice"])
        unit = row["unitOfMeasure"]
        divisor = {"1 Hour": 1, "1/Month": 730, "1 Month": 730, "1/Day": 24,
                   "1 Day": 24}.get(unit)
        if divisor is None:
            raise RuntimeError(f"Unsupported retail unit for {kind}: {unit}")
        prices[kind] = {"hourly_aud": row["retailPrice"] / divisor,
                        "meter": row["meterName"], "price_aud": row["retailPrice"],
                        "unit": unit}
    return prices


def estimate_cost(prices: dict, hours: float) -> dict:
    # OS, Qdrant, Airflow metadata/logs, Prometheus and temporary restore disk.
    # Conservatively quote each
    # small persistent disk at the existing 64Gi meter; never hide the addition.
    hourly = sum(prices[k]["hourly_aud"] * (6 if k == "disk" else 2 if k == "public_ip" else 1)
                 for k in prices)
    # Bounded tiny documents/traffic. Reserve AUD5 for Blob operations, egress,
    # monitoring ingestion, disk operations and price rounding/minimum billing.
    reserve = 5.0
    return {"currency": "AUD", "hours": hours, "hourly_aud": round(hourly, 4),
            "usage_reserve_aud": reserve, "estimated_aud": round(hourly * hours + reserve, 2),
            "quotes": prices, "is_hard_cap": False}


def managed_resources(state):
    def walk(module):
        yield from (r for r in module.get("resources", []) if r.get("mode") == "managed")
        for child in module.get("child_modules", []):
            yield from walk(child)
    return list(walk(state.get("values", {}).get("root_module", {})))


def free_capacity(config, managed):
    found = {}
    ids = {r["values"].get("id", "").lower() for r in managed}
    for kind in ("search", "cosmos"):
        settings = config.get(kind, {})
        supplied = settings.get("existing_account_id")
        subscription = supplied.split("/")[2] if supplied else settings.get("subscription_id", config["subscription_id"])
        account = az("account", "show", subscription=subscription)
        if account["tenantId"] != config["tenant_id"]:
            raise ValueError("All store subscriptions must use the workload identities' Entra tenant")
        if not supplied:
            command = ("search", "service", "list") if kind == "search" else ("cosmosdb", "list")
            accounts = az(*command, subscription=subscription)
            free = [a for a in accounts if (a.get("sku", {}).get("name", "").lower() == "free"
                                           if kind == "search" else a.get("enableFreeTier"))]
            foreign = [a for a in free if a["id"].lower() not in ids]
            if foreign:
                raise ValueError(f"{kind}: free account already exists; supply its resource ID for reuse")
            if not free:
                found[kind] = "No existing free account; Terraform must create the free tier"
                continue
            supplied = free[0]["id"]
        parts = supplied.split("/")
        group, name = parts[4], parts[8]
        if kind == "search":
            value = az("search", "service", "show", "-g", group, "-n", name, subscription=subscription)
            if value["sku"]["name"].lower() != "free":
                raise ValueError("Search must use the free tier")
            if not value.get("disableLocalAuth") and "aadOrApiKey" not in (value.get("authOptions") or {}):
                raise ValueError("Supplied Search must already support Entra; account settings are preserved")
            indexes = az("rest", "--method", "get", "--resource", "https://search.azure.com",
                         "--url", f"https://{name}.search.windows.net/indexes?api-version=2024-07-01&$select=name",
                         subscription=subscription)["value"]
            wanted = settings.get("index_name", "medw-chunks")
            exists = any(index["name"] == wanted for index in indexes)
            owned = any(r["type"] == "azapi_data_plane_resource" and r["values"].get("name") == wanted
                        and r["values"].get("parent_id") == f"{name}.search.windows.net" for r in managed)
            if exists and not owned:
                raise ValueError("Search index already exists outside this state; review/import it explicitly")
            if not exists and len(indexes) >= 3:
                raise ValueError("Free Search has no unused index slot")
            found[kind] = {"account": supplied, "indexes_used": len(indexes), "application_index_exists": exists}
        else:
            value = az("cosmosdb", "show", "-g", group, "-n", name, subscription=subscription)
            if (value.get("kind") != "GlobalDocumentDB" or not value.get("enableFreeTier")
                    or len(value.get("locations", [])) != 1
                    or any(c["name"] == "EnableServerless" for c in value.get("capabilities", []))):
                raise ValueError("Cosmos must be a single-region free NoSQL provisioned-throughput account")
            wanted = settings.get("database_name", "medw")
            databases = az("cosmosdb", "sql", "database", "list", "-g", group, "-a", name, subscription=subscription)
            total, current = 0, 0
            for db in databases:
                database = db["name"]
                if database == wanted and db["id"].lower() not in ids:
                    raise ValueError("Cosmos database already exists outside this state; review/import it explicitly")
                commands = [("database", "-n", database)]
                containers = az("cosmosdb", "sql", "container", "list", "-g", group, "-a", name,
                                "-d", database, subscription=subscription)
                commands += [("container", "-d", database, "-n", c["name"]) for c in containers]
                for command in commands:
                    offer = az("cosmosdb", "sql", command[0], "throughput", "show", "-g", group,
                               "-a", name, *command[1:], subscription=subscription, missing_offer=True)
                    resource = offer["resource"] if offer else {}
                    ru = int(resource.get("throughput") or resource.get("autoscaleSettings", {}).get("maxThroughput", 0))
                    total += ru
                    if database == wanted and command[0] == "database":
                        current = ru
            proposed = total - current + settings.get("throughput", 400)
            if proposed > 1000:
                raise ValueError(f"Shared Cosmos would use {proposed} RU/s; the free allowance is 1000")
            found[kind] = {"account": supplied, "current_ru": total, "proposed_ru": proposed,
                           "shared_storage_allowance_gb": 25}
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True, help="Environment Terraform .tfvars.json")
    parser.add_argument("--state-json", type=pathlib.Path, help="terraform show -json output, stored privately")
    parser.add_argument("--hours", type=float, default=4)
    parser.add_argument("--budget-aud", type=float, default=20)
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("data/evidence/preflight.json"))
    args = parser.parse_args()
    if not 0 < args.hours <= 12 or not 0 < args.budget_aud <= 20:
        parser.error("This commissioning check supports up to 12 hours and A$20; revise the cost policy explicitly for larger deployments")
    config = json.loads(args.config.read_text())
    managed = managed_resources(json.loads(args.state_json.read_text()) if args.state_json else {})
    account = az("account", "show", subscription=config["subscription_id"])
    if account["tenantId"] != config["tenant_id"]:
        raise ValueError("Subscription does not belong to the configured tenant")
    if config.get("node_vm_size", "Standard_D4s_v5") != "Standard_D4s_v5":
        raise ValueError("This price/quota check only covers the explicitly agreed Standard_D4s_v5 size")
    required = {"Microsoft.ContainerService", "Microsoft.ContainerRegistry", "Microsoft.Network",
                "Microsoft.Storage", "Microsoft.Sql", "Microsoft.Insights", "Microsoft.AlertsManagement",
                "Microsoft.OperationalInsights", "Microsoft.ManagedIdentity", "Microsoft.KeyVault"}
    providers = az("provider", "list", subscription=config["subscription_id"])
    registered = {p["namespace"].casefold() for p in providers if p["registrationState"] == "Registered"}
    missing = {namespace for namespace in required if namespace.casefold() not in registered}
    if missing:
        raise ValueError(f"Register required providers before applying: {sorted(missing)}")
    location = config.get("location", "australiaeast")
    usage = az("vm", "list-usage", "-l", location, subscription=config["subscription_id"])
    # An existing node only counts when it belongs to this Terraform state.
    cluster_id = next((r["values"]["id"] for r in managed if r["type"] == "azurerm_kubernetes_cluster"), None)
    needed = 4
    if cluster_id:
        cluster = az("resource", "show", "--ids", cluster_id, subscription=config["subscription_id"])
        pools = cluster["properties"]["agentPoolProfiles"]
        if len(pools) == 1 and pools[0]["vmSize"] == "Standard_D4s_v5" and pools[0]["count"] == 1:
            needed = 0
    for family in ("cores", "standardDSv5Family"):
        row = next((r for r in usage if r["name"]["value"].lower() == family.lower()), None)
        if row is None or int(row["limit"]) - int(row["currentValue"]) < needed:
            raise ValueError(f"Insufficient or unknown {family} quota for {needed} additional vCPUs")
    capacity = free_capacity(config, managed)
    cost = estimate_cost(retail_prices(location), args.hours)
    if cost["estimated_aud"] > args.budget_aud:
        raise ValueError(f"Estimated A${cost['estimated_aud']} exceeds A${args.budget_aud}")
    report = {"timestamp": datetime.now(UTC).isoformat(), "free_capacity": capacity,
              "cost": cost, "subscription": config["subscription_id"], "additional_vcpus": needed}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Preflight passed; estimated A${cost['estimated_aud']} for {args.hours} hours including a usage reserve. Not a spending cap.")
    print(f"Evidence: {args.output}")


if __name__ == "__main__":
    main()
