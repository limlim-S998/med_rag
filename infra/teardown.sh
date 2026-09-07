#!/usr/bin/env bash
# Delete everything. One command, because deleting has to be easier than
# forgetting - a resource group left running over a weekend is how a free
# trial becomes a bill.
#
# This is the other half of "a resource group is a folder with a lifecycle":
# the whole reason environments get their own group is that this works.
set -euo pipefail

ENVN=${MEDW_ENV:-dev}
RG=rg-medw-$ENVN

echo "About to delete resource group: $RG"
az resource list -g "$RG" --query "[].{name:name, type:type}" -o table || true
read -r -p "Type the group name to confirm: " confirm
[ "$confirm" = "$RG" ] || { echo "aborted"; exit 1; }

# --no-wait returns immediately; deletion continues server-side. Check with
#   az group exists -n $RG
az group delete -n "$RG" --yes --no-wait
echo "Deletion started. Nothing in $RG survives it, including the AOAI"
echo "deployments and every role assignment scoped inside it."
