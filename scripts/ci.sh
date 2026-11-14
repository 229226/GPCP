#!/usr/bin/env bash
# One-shot local CI: rebuild bundles, verify the frozen lock, run the backend
# conformance suite, unit tests, a small parameter sweep and the demo backend.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== build =="
python3 -m wlbench.cli build --all >/dev/null
echo "   ok"

echo "== verify (frozen 17 x 3 artifacts) =="
python3 -m wlbench.cli verify

echo "== conformance =="
python3 -m wlbench.cli conformance

echo "== unit tests =="
python3 -m unittest discover -s tests

echo "== sweep (mmse_scalable 2x2 points) =="
python3 -m wlbench.cli sweep --scenario mmse_scalable \
    --set layers=2,4 --set rx=2,4 | tail -5

echo "== demo backend =="
python3 examples/reference_backend.py --all | tail -4

echo "CI OK"
