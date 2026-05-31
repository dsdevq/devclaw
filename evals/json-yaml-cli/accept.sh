#!/usr/bin/env bash
# Acceptance check — run in the built workspace (cwd = the project root the
# agent created). Exit 0 = pass. Tests the hard contract from idea.txt:
# `python -m jyq` round-trips JSON → YAML → JSON without data loss.
set -euo pipefail

python -m pip install -q pyyaml >/dev/null 2>&1 || true

cat > _eval_sample.json <<'JSON'
{"name": "devclaw", "count": 3, "nums": [1, 2, 3], "nested": {"ok": true, "tags": ["a", "b"]}}
JSON

# json -> yaml -> json
python -m jyq to-yaml _eval_sample.json > _eval_sample.yaml
python -m jyq to-json _eval_sample.yaml > _eval_roundtrip.json

python - <<'PY'
import json
original = json.load(open("_eval_sample.json"))
roundtrip = json.load(open("_eval_roundtrip.json"))
assert original == roundtrip, f"round-trip mismatch:\n  in : {original}\n  out: {roundtrip}"
print("acceptance OK — JSON↔YAML round-trips losslessly")
PY
