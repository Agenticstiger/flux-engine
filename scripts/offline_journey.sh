#!/bin/sh
# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0
#
# A real command-line journey with no network at all: write a bundle from a profile, plan it, run
# it with verify and ship, tell one row's story, and check consumer queries. Every command must
# exit 0. The CI job "Offline journey" runs this inside `unshare --net`, a network namespace with
# a loopback interface and nothing else, so a command that opens a connection to anywhere fails
# here (docs/explanation/offline.md). It also runs without a namespace, on any machine.
#
# Usage: scripts/offline_journey.sh [WORKDIR]    (WORKDIR must not exist; default: a new temp dir)
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$(mktemp -d)}
state="$work/state"
contract="$repo/tests/fixtures/author/orders.fluid.yaml"
profile="$repo/tests/fixtures/author/orders-mean.profile.json"
mkdir -p "$work"

step() {
    printf '\n== %s\n' "$*"
}

step "no network interface but loopback"
python - <<'PY'
import socket
import sys

try:
    socket.create_connection(("192.0.2.1", 9), timeout=3)
except OSError as error:
    print(f"an outside address is unreachable, as it should be: {error}")
else:
    sys.exit("an outside address answered: this is not an offline machine")
PY

step "init, from a contract and a profile"
fluxsim init "$work/bundle" \
    --contract "$contract" --profile "$profile"

step "plan"
fluxsim plan "$work/bundle" --profile "$profile" --state-dir "$state"

step "run --verify --ship"
fluxsim run "$work/bundle" --profile "$profile" --verify --ship -o "$work/out" --state-dir "$state"

step "story"
run_id=$(find "$state/runs" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort | tail -n 1)
order_id=$(sed -n '1s/^\["\([^"]*\)".*/\1/p' "$state/runs/$run_id/emits/orders.ndjson")
echo "run $run_id, order $order_id"
fluxsim story --run-id "$run_id" --emit orders --key "order_id=$order_id" --state-dir "$state"

step "consumer queries (the sql extra)"
if python -c "import sqlglot, duckdb" 2>/dev/null; then
    fluxsim run "$repo/tests/fixtures/consumers/bundle" \
        --profile "$repo/tests/fixtures/consumers/subscriptions.profile.json" \
        --verify --state-dir "$work/state-consumers" | tee "$work/consumers.txt"
    grep -q "queries: calibration 1 of 1 within tolerance" "$work/consumers.txt"
else
    echo "the sql extra is not installed; skipped"
fi

step "done: every command exited 0 with no network"
