#!/bin/sh
# Load the demo secrets and contract addresses written by hs_demo.init, then run the command.
set -eu
if [ -f "${DEMO_STATE_DIR}/demo.env" ]; then
  set -a
  . "${DEMO_STATE_DIR}/demo.env"
  set +a
fi
exec "$@"
