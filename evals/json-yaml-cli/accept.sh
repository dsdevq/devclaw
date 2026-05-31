#!/usr/bin/env bash
# Acceptance check — run in the built workspace (cwd = the project root the
# agent created). Exit 0 = pass. Tests the hard contract from idea.txt:
# `python3 -m jyq` round-trips JSON → YAML → JSON without data loss.
#
# Uses $EVAL_PYTHON (the harness's own interpreter, which has PyYAML) so the
# check doesn't depend on a bare `python` being on PATH. Falls back to python3.
set -euo pipefail

PY="${EVAL_PYTHON:-python3}"
"$PY" -m pip install -q pyyaml >/dev/null 2>&1 || true

cat > _eval_sample.json <<'JSON'
{"name": "devclaw", "count": 3, "nums": [1, 2, 3], "nested": {"ok": true, "tags": ["a", "b"]}}
JSON

# json -> yaml -> json
"$PY" -m jyq to-yaml _eval_sample.json > _eval_sample.yaml
"$PY" -m jyq to-json _eval_sample.yaml > _eval_roundtrip.json

"$PY" - <<'PY'
import json
original = json.load(open("_eval_sample.json"))
roundtrip = json.load(open("_eval_roundtrip.json"))
assert original == roundtrip, f"round-trip mismatch:\n  in : {original}\n  out: {roundtrip}"
print("acceptance OK — JSON↔YAML round-trips losslessly")
PY
