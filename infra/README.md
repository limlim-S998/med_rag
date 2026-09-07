# infra

`bootstrap.sh` is deliberately imperative `az` CLI rather than Bicep or
Terraform. Reason: you should be able to read it once and understand the
shape of the account. Real environments here were Bicep in a separate repo
owned by the platform function - the deflection line for that is in the prep
doc ("subscription-level infrastructure was a platform function").

Useful reflex commands:

    az account list -o table
    az resource list -g rg-medw-dev -o table
    az cognitiveservices account deployment list -n medwdevaoai -g rg-medw-dev -o table
    az aks show -n medwdevaks -g rg-medw-dev --query oidcIssuerProfile.issuerUrl -o tsv
    az ml workspace show -n medw-dev-ws -g rg-medw-dev --query mlflow_tracking_uri -o tsv
