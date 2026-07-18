#!/usr/bin/env bash
# =============================================================================
# list_instances.sh — Liste toutes les instances Luciole et leur état
# Usage : bash scripts/list_instances.sh
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INSTANCES_DIR="$ROOT_DIR/instances"
REGISTRY="$INSTANCES_DIR/.registry"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "   Luciole — Instances installées"
echo "════════════════════════════════════════════════════════════"
echo ""

# LLM partagé
if docker ps --format '{{.Names}}' | grep -q "luciole-tensorrt-shared"; then
  echo -e "  LLM partagé (TRT-LLM) : \033[0;32m● actif\033[0m"
else
  echo -e "  LLM partagé (TRT-LLM) : \033[0;31m○ arrêté\033[0m"
fi
echo ""

if [ ! -f "$REGISTRY" ]; then
  echo "  Aucune instance installée."
  echo ""
  exit 0
fi

printf "  %-20s %-10s %-8s %-8s %-8s\n" "MÉTIER" "ÉTAT" "CHAT" "ADMIN" "API"
printf "  %-20s %-10s %-8s %-8s %-8s\n" "──────────────────" "──────────" "──────" "──────" "──────"

while IFS='|' read -r name api admin chat feedback qdrant opensearch watcher msmtp mimap madmin; do
  if docker ps --format '{{.Names}}' | grep -q "luciole-agent-$name"; then
    state="\033[0;32m● actif\033[0m"
  else
    state="\033[0;31m○ arrêté\033[0m"
  fi
  printf "  %-20s " "$name"
  echo -e "$state $(printf '%-8s %-8s %-8s' "$chat" "$admin" "$api")"
done < "$REGISTRY"

echo ""
