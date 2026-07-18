#!/usr/bin/env bash
# =============================================================================
# install_gx10.sh — Installeur interactif Luciole multi-instances (GX10)
#
# Ce script :
#   1. Demande le nom du métier (INSTANCE_NAME)
#   2. Détecte les ports déjà utilisés par d'autres instances
#   3. Calcule les ports libres pour la nouvelle instance
#   4. Génère le fichier .env de l'instance dans instances/<metier>/
#   5. Lance le stack métier (sans TRT-LLM — partagé)
#
# Prérequis :
#   - Le stack LLM partagé doit être démarré :
#     docker compose -f docker-compose.shared-llm.yml \
#                    -f docker-compose.shared-llm.gx10.yml up -d
#   - L'image luciole-gpu:arm64 doit être buildée :
#     docker build -f Dockerfile.gpu.arm64 -t luciole-gpu:arm64 .
#
# Usage :
#   sudo bash scripts/install_gx10.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INSTANCES_DIR="$ROOT_DIR/instances"

# Couleurs
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }

# ─── Vérifications préalables ────────────────────────────────────────────────

if [ "$EUID" -ne 0 ]; then
  error "Ce script doit être lancé avec sudo : sudo bash scripts/install_gx10.sh"
fi

if ! command -v docker &>/dev/null; then
  error "Docker n'est pas installé."
fi

# Vérifie que le réseau partagé existe (LLM partagé lancé)
if ! docker network ls --format '{{.Name}}' | grep -q "^luciole_shared$"; then
  warn "Le réseau 'luciole_shared' n'existe pas."
  warn "Lancez d'abord le stack LLM partagé :"
  warn "  docker compose -f docker-compose.shared-llm.yml -f docker-compose.shared-llm.gx10.yml up -d"
  read -rp "Continuer quand même ? (o/N) : " CONT
  [[ "$CONT" =~ ^[Oo]$ ]] || exit 0
fi

# ─── Demande du nom du métier ─────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════════════"
echo "   Luciole — Installation d'une nouvelle instance métier"
echo "════════════════════════════════════════════════════════════"
echo ""

# Liste les instances existantes
if [ -d "$INSTANCES_DIR" ] && [ "$(ls -A "$INSTANCES_DIR" 2>/dev/null)" ]; then
  info "Instances déjà installées :"
  for d in "$INSTANCES_DIR"/*/; do
    name=$(basename "$d")
    status="arrêtée"
    if docker ps --format '{{.Names}}' | grep -q "luciole-agent-$name"; then
      status="${GREEN}active${NC}"
    fi
    echo -e "   • $name — $status"
  done
  echo ""
fi

while true; do
  read -rp "Pour quel métier / client ? (ex: support, juridique, chavenay) : " METIER
  METIER=$(echo "$METIER" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/_/g' | sed 's/^[0-9]/idx_&/')
  METIER="${METIER:0:32}"

  if [ -z "$METIER" ]; then
    warn "Le nom ne peut pas être vide."
    continue
  fi

  if [ -d "$INSTANCES_DIR/$METIER" ]; then
    warn "Une instance '$METIER' existe déjà dans $INSTANCES_DIR/$METIER"
    read -rp "Reconfigurer / relancer cette instance ? (o/N) : " RECONF
    if [[ "$RECONF" =~ ^[Oo]$ ]]; then
      break
    fi
    continue
  fi
  break
done

info "Nom du métier : '$METIER'"

# ─── Détection des ports occupés ─────────────────────────────────────────────

info "Détection des ports déjà utilisés..."

# Ports utilisés par Docker (hôte)
USED_PORTS=$(docker ps --format '{{.Ports}}' | grep -oP '\d+(?=->)' | sort -n | uniq)

# Aussi les ports en écoute sur l'hôte
USED_PORTS+=" $(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -oP '\d+$' | sort -n | uniq)"

find_free_port() {
  local start=$1
  local port=$start
  while echo "$USED_PORTS" | grep -qw "$port"; do
    ((port++))
  done
  USED_PORTS+=" $port"
  echo $port
}

# Plages de base : chaque instance occupe un bloc de 10 ports
# Instance 1 : 8000-8009 | Instance 2 : 8010-8019 | etc.
# On cherche le premier bloc libre à partir de 8000

BASE=8000
while true; do
  CONFLICT=0
  for offset in 0 1 2 3 4 5 6 7 8 9; do
    if echo "$USED_PORTS" | grep -qw "$((BASE + offset))"; then
      CONFLICT=1
      break
    fi
  done
  if [ $CONFLICT -eq 0 ]; then
    break
  fi
  BASE=$((BASE + 10))
done

API_PORT=$BASE
ADMIN_PORT=$((BASE + 1))
CHAT_PORT=$((BASE + 2))
FEEDBACK_PORT=$((BASE + 3))
QDRANT_PORT=$((BASE + 4))
OPENSEARCH_PORT=$((BASE + 5))
WATCHER_PORT=$((BASE + 6))
MAIL_SMTP_PORT=$((BASE + 7))
MAIL_IMAP_PORT=$((BASE + 8))
MAIL_ADMIN_PORT=$((BASE + 9))

echo ""
info "Ports assignés à l'instance '$METIER' :"
echo "   API (agent)    : $API_PORT"
echo "   Admin UI       : $ADMIN_PORT"
echo "   Chat UI        : $CHAT_PORT"
echo "   Feedback UI    : $FEEDBACK_PORT"
echo "   Qdrant         : $QDRANT_PORT"
echo "   OpenSearch     : $OPENSEARCH_PORT"
echo "   Watcher        : $WATCHER_PORT"
echo "   Mail SMTP      : $MAIL_SMTP_PORT"
echo "   Mail IMAP      : $MAIL_IMAP_PORT"
echo "   Mail Admin     : $MAIL_ADMIN_PORT"
echo ""

read -rp "Confirmer l'installation ? (O/n) : " CONFIRM
[[ "$CONFIRM" =~ ^[Nn]$ ]] && exit 0

# ─── Création du répertoire d'instance ───────────────────────────────────────

INSTANCE_DIR="$INSTANCES_DIR/$METIER"
mkdir -p "$INSTANCE_DIR"/{data,config,feedbacks,backups,models/huggingface,evaluation}

# Lien symbolique vers les modèles partagés (embeddings)
if [ -d "$ROOT_DIR/models/huggingface" ] && [ ! -L "$INSTANCE_DIR/models/huggingface" ]; then
  rm -rf "$INSTANCE_DIR/models/huggingface"
  ln -s "$ROOT_DIR/models/huggingface" "$INSTANCE_DIR/models/huggingface"
  info "Lien symbolique créé : models/huggingface → $ROOT_DIR/models/huggingface"
fi

# Copie de la config par défaut si absente
if [ ! -f "$INSTANCE_DIR/config/settings.yaml" ] && [ -f "$ROOT_DIR/config/settings.yaml.example" ]; then
  cp "$ROOT_DIR/config/settings.yaml.example" "$INSTANCE_DIR/config/settings.yaml"
  info "Config copiée : config/settings.yaml"
fi

# Génération de la clé de chiffrement mail
MAIL_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "")

# ─── Génération du .env de l'instance ────────────────────────────────────────

cat > "$INSTANCE_DIR/.env" <<EOF
# =============================================================================
# .env — Instance Luciole : $METIER
# Généré par install_gx10.sh le $(date '+%Y-%m-%d %H:%M:%S')
# =============================================================================

# ── Instance ──────────────────────────────────────────────────────────────────
INSTANCE_NAME=$METIER

# ── Timezone ──────────────────────────────────────────────────────────────────
TZ=Europe/Paris

# ── Ports ─────────────────────────────────────────────────────────────────────
API_PORT=$API_PORT
ADMIN_PORT=$ADMIN_PORT
CHAT_PORT=$CHAT_PORT
FEEDBACK_PORT=$FEEDBACK_PORT
QDRANT_PORT=$QDRANT_PORT
OPENSEARCH_PORT=$OPENSEARCH_PORT
WATCHER_PORT=$WATCHER_PORT
MAIL_SMTP_PORT=$MAIL_SMTP_PORT
MAIL_IMAP_PORT=$MAIL_IMAP_PORT
MAIL_ADMIN_PORT=$MAIL_ADMIN_PORT

# ── LLM partagé (ne pas modifier) ────────────────────────────────────────────
# Le TRT-LLM tourne dans le stack partagé sur le réseau luciole_shared
LLM_URL=http://tensorrt-llm-shared:8000

# ── Modèles partagés (embeddings) ───────────────────────────────────────────
HF_MODELS_PATH=$ROOT_DIR/models/huggingface

# ── Watcher ───────────────────────────────────────────────────────────────────
WATCHER_ENABLED=true

# ── Sécurité mail ─────────────────────────────────────────────────────────────
MAIL_ENCRYPTION_KEY=$MAIL_KEY
EOF

success ".env généré : $INSTANCE_DIR/.env"

# ─── Lancement du stack ───────────────────────────────────────────────────────

info "Lancement du stack '$METIER'..."

cd "$INSTANCE_DIR"

# Liens symboliques vers les compose files partagés
ln -sf "$ROOT_DIR/docker-compose.instance.yml" ./docker-compose.yml
ln -sf "$ROOT_DIR/docker-compose.instance.gx10.yml" ./docker-compose.gx10.yml
ln -sf "$ROOT_DIR/rag-system" ./rag-system 2>/dev/null || true
ln -sf "$ROOT_DIR/Dockerfile.gpu.arm64" ./Dockerfile.gpu.arm64 2>/dev/null || true

docker compose \
  -f docker-compose.yml \
  -f docker-compose.gx10.yml \
  --project-name "luciole-$METIER" \
  --profile gpu \
  up -d

echo ""
success "Instance '$METIER' démarrée !"
echo ""
echo "   Chat UI    → http://localhost:$CHAT_PORT"
echo "   Admin UI   → http://localhost:$ADMIN_PORT"
echo "   Feedback   → http://localhost:$FEEDBACK_PORT"
echo "   API        → http://localhost:$API_PORT"
echo ""
echo "   Pour arrêter : sudo bash scripts/stop_instance.sh $METIER"
echo "   Pour les logs : docker compose --project-name luciole-$METIER logs -f"
echo ""

# Sauvegarde du registre des instances
REGISTRY="$INSTANCES_DIR/.registry"
# Supprimer l'entrée existante si présente
grep -v "^$METIER|" "$REGISTRY" 2>/dev/null > "$REGISTRY.tmp" || true
echo "$METIER|$API_PORT|$ADMIN_PORT|$CHAT_PORT|$FEEDBACK_PORT|$QDRANT_PORT|$OPENSEARCH_PORT|$WATCHER_PORT|$MAIL_SMTP_PORT|$MAIL_IMAP_PORT|$MAIL_ADMIN_PORT" >> "$REGISTRY.tmp"
mv "$REGISTRY.tmp" "$REGISTRY"
