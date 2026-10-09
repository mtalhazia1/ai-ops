#!/usr/bin/env bash
# Import every workflow in n8n/workflows/, publish the ones with triggers, restart n8n.
# Usage: scripts/import_workflows.sh [extra compose -f files...]
#   e.g. scripts/import_workflows.sh docker-compose.prod.yml
set -euo pipefail
cd "$(dirname "$0")/.."
files=(-f docker-compose.yml)
for f in "$@"; do files+=(-f "$f"); done
dc() { docker compose "${files[@]}" "$@"; }

for wf in n8n/workflows/*.json; do
  name=$(basename "$wf")
  if out=$(dc exec -T n8n n8n import:workflow --input="/workflows/$name" 2>&1); then
    echo "Imported $name"
  else
    echo "Import of $name failed:"; echo "$out" | tail -20; exit 1
  fi
done
# WF1 polls Gmail: publish it yourself once the Gmail credential and label IDs are set.
# WF4 must be published too: n8n only runs a published error workflow.
for id in wf2process000001 wf3execute000001 wf4error00000001 wf5digest0000001; do
  dc exec -T n8n n8n publish:workflow --id="$id" >/dev/null 2>&1 && echo "Published $id"
done
dc restart n8n n8n-worker >/dev/null 2>&1
echo "Done. Open the n8n editor, attach credentials where nodes show a warning, then publish WF1."
