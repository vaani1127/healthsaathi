#!/bin/sh
# Start anvil if it is not running, then deploy the contracts to it.
# Uses anvil's unlocked default accounts, so no private key is needed.
set -eu
cd "$(dirname "$0")/.."

RPC_URL="${ANVIL_RPC_URL:-http://127.0.0.1:8545}"

if ! cast chain-id --rpc-url "$RPC_URL" >/dev/null 2>&1; then
  echo "starting anvil on $RPC_URL"
  nohup anvil --silent </dev/null >.anvil.log 2>&1 &
  echo $! >.anvil.pid
  tries=0
  until cast chain-id --rpc-url "$RPC_URL" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 100 ]; then
      echo "anvil did not start, see contracts/.anvil.log" >&2
      exit 1
    fi
    sleep 0.2
  done
fi

# The first three anvil dev accounts (public, used only on the local chain).
export DEPLOY_NETWORK=anvil
export REGISTRY_ADMIN_1=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
export REGISTRY_ADMIN_2=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
export REGISTRY_ADMIN_3=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC

forge script script/Deploy.s.sol \
  --rpc-url "$RPC_URL" \
  --broadcast \
  --unlocked \
  --sender "$REGISTRY_ADMIN_1"

echo "deployed:"
cat deployments/anvil.json
echo
