#!/usr/bin/env bash
# =============================================================================
# download_embeddings.sh — Téléchargement des modèles d'embedding et reranker
#                          nécessaires à la stack RAG Luciole (GX10 / arm64)
#
# Modèles téléchargés :
#   - BAAI/bge-m3          : embedding dense multilingue (~2.3 Go)
#   - BAAI/bge-reranker-v2-m3 : reranker cross-encoder (~2.3 Go)
#
# Usage : bash scripts/download_embeddings.sh
#
# Appelé automatiquement par prepare_gx10.sh (étape 2.5)
# Peut être relancé sans risque : reprend là où il s'est arrêté.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
HF_CACHE="${PROJECT_ROOT}/models/huggingface/hub"

MODELS=(
    "BAAI/bge-m3"
    "BAAI/bge-reranker-v2-m3"
)

echo "============================================================"
echo "  Téléchargement modèles embedding/reranker — Luciole RAG"
echo "  Cache : ${HF_CACHE}"
echo "============================================================"

# ── Prérequis : Python + huggingface_hub (via venv — évite PEP 668) ──────────
REAL_USER="${SUDO_USER:-$(whoami)}"
VENV_PATH="/home/${REAL_USER}/luciole-venv"
if [[ ! -f "${VENV_PATH}/bin/python3" ]]; then
    echo "[INFO] Création du venv Python dans ${VENV_PATH}..."
    sudo -u "${REAL_USER}" python3 -m venv "${VENV_PATH}"
fi
if ! "${VENV_PATH}/bin/python3" -c "import huggingface_hub" 2>/dev/null; then
    echo "[INFO] Installation de huggingface_hub dans le venv..."
    "${VENV_PATH}/bin/pip" install --quiet huggingface_hub
fi
PYTHON="${VENV_PATH}/bin/python3"

# ── Création du dossier cache avec les bonnes permissions ────────────────────
mkdir -p "${HF_CACHE}"
# S'assurer que l'utilisateur courant peut écrire (corrige le cas sudo)
if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "${SUDO_USER}:${SUDO_USER}" "${PROJECT_ROOT}/models/huggingface"
fi

# ── Téléchargement ────────────────────────────────────────────────────────────
ALL_OK=true
for REPO in "${MODELS[@]}"; do
    LOCAL_NAME="models--${REPO//\/\/--}"
    LOCAL_NAME="models--${REPO/\//--}"
    LOCAL_DIR="${HF_CACHE}/${LOCAL_NAME}"

    if [[ -f "${LOCAL_DIR}/config.json" ]] || [[ -f "${LOCAL_DIR}/tokenizer_config.json" ]]; then
        echo "[OK] Déjà présent : ${REPO}"
        continue
    fi

    echo "[INFO] Téléchargement : ${REPO} → ${LOCAL_DIR}"
    "${PYTHON}" - <<PYEOF
from huggingface_hub import snapshot_download
import warnings
warnings.filterwarnings("ignore")
snapshot_download(
    repo_id="${REPO}",
    local_dir="${LOCAL_DIR}",
)
print("[OK] ${REPO} téléchargé.")
PYEOF
    if [[ $? -ne 0 ]]; then
        echo "[ERREUR] Échec téléchargement : ${REPO}"
        ALL_OK=false
    fi
done

# ── Fix permissions finales ──────────────────────────────────────────────────
if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "${SUDO_USER}:${SUDO_USER}" "${PROJECT_ROOT}/models/huggingface"
fi

if [[ "${ALL_OK}" == true ]]; then
    echo ""
    echo "============================================================"
    echo "  Modèles embedding/reranker prêts."
    echo "  Chemin : ${HF_CACHE}"
    echo "============================================================"
else
    echo "[ATTENTION] Certains modèles n'ont pas pu être téléchargés."
    exit 1
fi
