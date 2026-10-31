#!/bin/sh
# Creates the prod-demo resources on Azure for Students, using the names in .env.prod. Safe to run
# again: anything that exists is left as it is. Needs the Azure CLI and `az login`.
#
#   sh infra/deploy/azure.sh provision   # resource group, VM, database, storage
#   sh infra/deploy/azure.sh open-db     # let this computer reach the database (seed, migrations)
#   sh infra/deploy/azure.sh close-db    # remove that rule again
set -eu

ENV_FILE="${HOSTED_ENV:-.env.prod}"
STATE="${HOSTED_STATE:-.demo/hosted}"
value() { grep "^$1=" "$ENV_FILE" | cut -d= -f2-; }
set_value() { uv run python -m hs_demo.hosted set "$1" "$2"; }

SUFFIX="$(value AZURE_SUFFIX)"
LOCATION="$(value AZURE_LOCATION)"
GROUP=rg-healthsaathi
VM=healthsaathi-vm
PG="healthsaathi-$SUFFIX"
STORAGE="hssaathi$SUFFIX"
ADMIN_USER=azureuser
VM_SIZE="${VM_SIZE:-Standard_B2ats_v2}"

provision() {
  mkdir -p "$STATE"
  [ -f "$STATE/deploy_key" ] ||
    ssh-keygen -q -t ed25519 -N "" -C healthsaathi-deploy -f "$STATE/deploy_key"

  # A new subscription must register each service before first use.
  for ns in Microsoft.Compute Microsoft.Network Microsoft.DBforPostgreSQL Microsoft.Storage; do
    az provider register --namespace "$ns" --wait --output none
  done
  az group show --name "$GROUP" --output none 2>/dev/null ||
    az group create --name "$GROUP" --location "$LOCATION" --output none

  echo "creating the VM"
  if ! az vm show -g "$GROUP" -n "$VM" --output none 2>/dev/null; then
    az vm create -g "$GROUP" -n "$VM" --location "$LOCATION" --image Ubuntu2404 --size "$VM_SIZE" \
      --admin-username "$ADMIN_USER" --ssh-key-values "$STATE/deploy_key.pub" \
      --authentication-type ssh --public-ip-sku Standard --os-disk-size-gb 64 \
      --storage-sku Premium_LRS --custom-data infra/deploy/cloud-init.yaml --output none
    # Port 22 stays open because GitHub-hosted runners deploy over SSH from changing addresses.
    # Only the key works and fail2ban blocks repeat attempts.
    az vm open-port -g "$GROUP" -n "$VM" --port 80,443 --priority 900 --output none
  fi
  IP="$(az vm show -d -g "$GROUP" -n "$VM" --query publicIps --output tsv | tr -d '\r')"
  set_value VM_HOST "$IP"
  set_value VM_USER "$ADMIN_USER"

  echo "creating the database (takes about 5 minutes)"
  if ! az postgres flexible-server show -g "$GROUP" -n "$PG" --output none 2>/dev/null; then
    az postgres flexible-server create -g "$GROUP" -n "$PG" --location "$LOCATION" \
      --admin-user hsadmin --admin-password "$(value DB_ADMIN_PASSWORD)" \
      --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
      --zonal-resiliency Disabled --public-access None --yes --output none
    az postgres flexible-server db create -g "$GROUP" --server-name "$PG" \
      --name healthsaathi --output none
  fi
  # Public endpoint on, but only the firewall rules below may connect.
  az postgres flexible-server update -g "$GROUP" -n "$PG" --public-access Enabled --output none
  az postgres flexible-server firewall-rule create -g "$GROUP" --server-name "$PG" --name vm \
    --start-ip-address "$IP" --end-ip-address "$IP" --output none

  echo "creating blob storage"
  if ! az storage account show -g "$GROUP" -n "$STORAGE" --output none 2>/dev/null; then
    az storage account create -g "$GROUP" -n "$STORAGE" --location "$LOCATION" \
      --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false \
      --min-tls-version TLS1_2 --output none
  fi
  KEY="$(az storage account keys list -g "$GROUP" -n "$STORAGE" --query '[0].value' \
    --output tsv | tr -d '\r')"
  az storage container create --name lab-docs --account-name "$STORAGE" --account-key "$KEY" \
    --output none
  EXPIRY="$(date -u -d '+2 years' +%Y-%m-%dT00:00Z)"
  SAS="$(az storage container generate-sas --name lab-docs --account-name "$STORAGE" \
    --account-key "$KEY" --permissions rcw --expiry "$EXPIRY" --https-only --output tsv |
    tr -d '\r')"
  set_value AZURE_BLOB_CONTAINER_SAS_URL "https://$STORAGE.blob.core.windows.net/lab-docs?$SAS"

  ssh-keyscan -t ed25519 "$IP" >"$STATE/known_hosts" 2>/dev/null || true
  echo "done. VM address: $IP"
}

open_db() {
  HERE="$(curl -fsS https://api.ipify.org)"
  az postgres flexible-server firewall-rule create -g "$GROUP" --server-name "$PG" \
    --name setup-laptop --start-ip-address "$HERE" --end-ip-address "$HERE" --output none
  echo "database open to $HERE"
}

close_db() {
  az postgres flexible-server firewall-rule delete -g "$GROUP" --server-name "$PG" \
    --name setup-laptop --yes --output none
  echo "database closed to this computer"
}

case "${1:-}" in
  provision) provision ;;
  open-db) open_db ;;
  close-db) close_db ;;
  *)
    echo "usage: azure.sh provision|open-db|close-db" >&2
    exit 2
    ;;
esac
