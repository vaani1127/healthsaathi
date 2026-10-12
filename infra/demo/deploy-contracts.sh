#!/bin/sh
# Deploy ClinicRegistry, AuditAnchor and ConsentRegistry to the demo anvil chain, once.
# Uses anvil's unlocked dev accounts (local chain only), so no private key is involved.
set -eu
RPC_URL="${ANVIL_RPC_URL:-http://anvil:8545}"
OUT="${DEMO_STATE_DIR:-/state}/anvil.json"

if [ -f "$OUT" ]; then
  address=$(sed -n 's/.*"AuditAnchor": *"\(0x[0-9a-fA-F]*\)".*/\1/p' "$OUT")
  if [ -n "$address" ] && [ "$(cast code "$address" --rpc-url "$RPC_URL")" != "0x" ]; then
    echo "[contracts] already deployed at $address"
    exit 0
  fi
fi

admins=$(cast rpc eth_accounts --rpc-url "$RPC_URL" | tr -d '[]" ' )
export DEPLOY_NETWORK=anvil
export REGISTRY_ADMIN_1=$(echo "$admins" | cut -d, -f1)
export REGISTRY_ADMIN_2=$(echo "$admins" | cut -d, -f2)
export REGISTRY_ADMIN_3=$(echo "$admins" | cut -d, -f3)
forge script script/Deploy.s.sol --rpc-url "$RPC_URL" --broadcast --unlocked \
  --sender "$REGISTRY_ADMIN_1" --silent
mkdir -p "$(dirname "$OUT")"
cp deployments/anvil.json "$OUT"
echo "[contracts] deployed:"
cat "$OUT"
