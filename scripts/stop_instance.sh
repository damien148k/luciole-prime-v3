#!/usr/bin/env bash
# =============================================================================
# stop_instance.sh — Arrête une instance métier Luciole
# Usage : sudo bash scripts/stop_instance.sh <metier>
# =============================================================================

set -euo pipefail

METIER="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INSTANCES_DIR="$ROOT_DIR/instances"

if [ -z "$METIER" ]; then
  echo "Usage : sudo bash scripts/stop_instance.sh <metier>"
  echo ""
  echo "Instances disponibles :"
  for d in "$INSTANCES_DIR"/*/; do
    echo "  - $(basename "$d")"
  done
  exit 1
fi

INSTANCE_DIR="$INSTANCES_DIR/$METIER"
if [ ! -d "$INSTANCE_DIR" ]; then
  echo "Instance '$METIER' introuvable dans $INSTANCES_DIR"
  exit 1
fi

cd "$INSTANCE_DIR"
docker compose --project-name "luciole-$METIER" down
echo "Instance '$METIER' arrêtée."
