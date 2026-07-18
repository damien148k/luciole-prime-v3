#!/usr/bin/env bash
# =============================================================================
# download_model.sh — Téléchargement de Qwen3-30B-A3B-Instruct-2507 NVFP4
#                     depuis HuggingFace (cible : GX10 / DGX Spark, arm64)
#
# Usage : bash scripts/download_model.sh [--token HF_TOKEN]
#
# Prérequis :
#   - Python 3.10+ avec huggingface_hub installé (dans un venv ou system)
#   - ~20 Go d'espace disque libre dans models/hf_models/
#   - Token HF facultatif (recommandé pour rate limit plus généreux)
#
# Variables d'environnement :
#   HF_REPO          — repo HuggingFace (défaut : NVFP4/Qwen3-30B-A3B-Instruct-2507-FP4)
#   MODEL_LOCAL_NAME — nom du dossier local (défaut : Qwen3-30B-A3B-Instruct-2507-NVFP4)
#   HF_TOKEN         — token HuggingFace (optionnel)
# =============================================================================

set -euo pipefail

# ── Répertoires ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Modèle NVFP4 (GX10 / DGX Spark) ──────────────────────────────────────────
HF_REPO="${HF_REPO:-NVFP4/Qwen3-30B-A3B-Instruct-2507-FP4}"
MODEL_LOCAL_NAME="${MODEL_LOCAL_NAME:-Qwen3-30B-A3B-Instruct-2507-NVFP4}"
HF_MODEL_DIR="${PROJECT_ROOT}/models/hf_models/${MODEL_LOCAL_NAME}"

# ── Arguments ──────────────────────────────────────────────────────────────────
HF_TOKEN="${HF_TOKEN:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) HF_TOKEN="$2"; shift 2 ;;
    *) echo "Option inconnue : $1"; exit 1 ;;
  esac
done

echo "============================================================"
echo "  Téléchargement du modèle LLM — GX10 (DGX Spark, GB10)"
echo "  Modèle : Qwen3-30B-A3B-Instruct-2507 NVFP4 (MoE ~3B actifs)"
echo "  Source : ${HF_REPO}"
echo "  Dest   : ${HF_MODEL_DIR}"
echo "============================================================"

# ── Vérification architecture ──────────────────────────────────────────────────
if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "[ATTENTION] Ce script est conçu pour le GX10 (aarch64). Détecté : $(uname -m)"
  read -rp "Continuer quand même ? (y/N) " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
fi

# ── Vérification Python + huggingface_hub ─────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "[ERREUR] Python3 requis. Installez Python 3.10+."
  exit 1
fi

if ! python3 -c "import huggingface_hub" 2>/dev/null; then
  echo "[INFO] Installation de huggingface_hub..."
  pip install --quiet "huggingface_hub[hf_transfer]"
fi

# ── Vérification espace disque ─────────────────────────────────────────────────
mkdir -p "${PROJECT_ROOT}/models/hf_models"
AVAILABLE_GB=$(df -BG "${PROJECT_ROOT}/models/hf_models" | awk 'NR==2 {gsub("G",""); print $4}')
echo "[INFO] Espace disponible : ${AVAILABLE_GB} Go (requis : ~20 Go)"
if [[ "${AVAILABLE_GB}" -lt 20 ]]; then
  echo "[ATTENTION] Espace disque insuffisant. Minimum recommandé : 20 Go."
  read -rp "Continuer quand même ? (y/N) " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
fi

# ── Modèle déjà présent ? ─────────────────────────────────────────────────────
if [[ -f "${HF_MODEL_DIR}/config.json" ]]; then
  echo "[OK] Modèle déjà présent : ${HF_MODEL_DIR}"
  echo "     Supprimez le dossier pour forcer un re-téléchargement."
  exit 0
fi

# ── Téléchargement ─────────────────────────────────────────────────────────────
echo "[INFO] Démarrage du téléchargement (~18-20 Go — reprend automatiquement si interrompu)..."
export HF_HUB_ENABLE_HF_TRANSFER=1

python3 - <<PY
import os, warnings
warnings.filterwarnings("ignore")
from huggingface_hub import snapshot_download

kw = dict(
    repo_id="${HF_REPO}",
    local_dir="${HF_MODEL_DIR}",
)
tok = "${HF_TOKEN}"
if tok:
    kw["token"] = tok

snapshot_download(**kw)
print("[OK] Téléchargement terminé.")
PY

# ── Vérification fichiers clés ─────────────────────────────────────────────────
REQUIRED_FILES=("config.json" "tokenizer.json" "tokenizer_config.json")
for f in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${HF_MODEL_DIR}/${f}" ]]; then
    echo "[ERREUR] Fichier manquant après téléchargement : ${f}"
    exit 1
  fi
done

echo ""
echo "============================================================"
echo "  Modèle téléchargé avec succès !"
echo "  Chemin : ${HF_MODEL_DIR}"
echo ""
echo "  Prochaine étape :"
echo "    bash scripts/prepare_gx10.sh"
echo "============================================================"
