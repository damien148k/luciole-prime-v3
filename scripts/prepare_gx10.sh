#!/usr/bin/env bash
# =============================================================================
# prepare_gx10.sh — Préparation du service LLM sur le GX10 (DGX Spark, GB10)
#
# ⚠️ À EXÉCUTER SUR LE GX10 (arm64), PAS sur le PC Windows.
#
# Modèle : Qwen3-30B-A3B-Instruct-2507 en NVFP4 (MoE ~3B actifs/token — sweet spot GB10).
# Variant Instruct-2507 : non-thinking par défaut (idéal CRM/support/RAG), raisonnement à la carte.
#
# Avec TRT-LLM >= 1.2 backend PyTorch, il n'y a PLUS de compilation d'engine :
# on sert directement le checkpoint HF DÉJÀ quantifié NVFP4. Ce script :
#   1. vérifie l'environnement GB10 (driver, CUDA, arch aarch64),
#   2. télécharge le checkpoint NVFP4 s'il est absent,
#   3. écrit un fichier d'options avancées (cuda graphs, overlap scheduler),
#   4. lance un WARMUP (1er run = compilation/caching des kernels sm_121).
#
# Usage :
#   HF_TOKEN=hf_xxx bash scripts/prepare_gx10.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Modèle NVFP4 ──────────────────────────────────────────────────────────────
# Repo NVIDIA officiel (base). Pour un usage RAG/chat, préférer une variante
# Instruct NVFP4 si tu la souhaites (voir README, section "Choix du repo").
HF_REPO="${HF_REPO:-NVFP4/Qwen3-30B-A3B-Instruct-2507-FP4}"
MODEL_LOCAL_NAME="${MODEL_LOCAL_NAME:-Qwen3-30B-A3B-Instruct-2507-NVFP4}"
HF_DIR="${PROJECT_ROOT}/models/hf_models/${MODEL_LOCAL_NAME}"

# ── Création et fix permissions des dossiers du projet ───────────────────────
# Docker crée ces dossiers en root — on s'assure que l'utilisateur courant peut écrire.
REAL_USER="${SUDO_USER:-$(whoami)}"
for dir in     "${PROJECT_ROOT}/models/huggingface/hub"     "${PROJECT_ROOT}/data"     "${PROJECT_ROOT}/feedbacks"     "${PROJECT_ROOT}/backups"     "${PROJECT_ROOT}/evaluation"     "${PROJECT_ROOT}/config"; do
    mkdir -p "$dir"
    chown -R "${REAL_USER}:${REAL_USER}" "$dir" 2>/dev/null || true
done
echo "[INFO] Permissions dossiers projet OK (owner: ${REAL_USER})"
TRT_IMAGE="${TRT_IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:1.2.0rc2}"
HF_TOKEN="${HF_TOKEN:-}"

echo "============================================================"
echo "  Préparation LLM GX10 (GB10 / sm_121) — Qwen3-30B-A3B-Instruct-2507 NVFP4"
echo "  Repo HF       : ${HF_REPO}"
echo "  Image TRT-LLM : ${TRT_IMAGE}"
echo "  Checkpoint    : ${HF_DIR}"
echo "============================================================"

# ── 1. Environnement ──────────────────────────────────────────────────────────
echo ""
echo "[1/4] Vérification de l'environnement GB10..."
if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "[ERREUR] Ce script doit tourner sur le GX10 (aarch64). Détecté : $(uname -m)"
  exit 1
fi
if ! command -v nvidia-smi &>/dev/null; then
  echo "[ERREUR] nvidia-smi introuvable — driver NVIDIA + Container Toolkit requis."
  exit 1
fi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
echo "[INFO] Test accès GPU depuis un container CUDA arm64..."
docker run --rm --gpus all nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi -L

# ── 2. Téléchargement du checkpoint NVFP4 ─────────────────────────────────────
echo ""
echo "[2/4] Vérification / téléchargement du checkpoint NVFP4..."
if [[ -f "${HF_DIR}/config.json" ]]; then
  echo "[OK] Modèle déjà présent : ${HF_DIR}"
else
  echo "[INFO] Téléchargement de ${HF_REPO} (~18-20 Go)..."
  # Créer/réutiliser le venv dédié (évite PEP 668 / externally-managed-environment)
  VENV_PATH="/home/${REAL_USER}/luciole-venv"
  if [[ ! -f "${VENV_PATH}/bin/python3" ]]; then
    echo "[INFO] Création du venv Python dans ${VENV_PATH}..."
    sudo -u "${REAL_USER}" python3 -m venv "${VENV_PATH}"
  fi
  if ! "${VENV_PATH}/bin/python3" -c "import huggingface_hub" 2>/dev/null; then
    echo "[INFO] Installation de huggingface_hub dans le venv..."
    "${VENV_PATH}/bin/pip" install --quiet "huggingface_hub[hf_transfer]"
  fi
  PYTHON="${VENV_PATH}/bin/python3"
  export HF_HUB_ENABLE_HF_TRANSFER=1
  mkdir -p "${HF_DIR}"
  "${PYTHON}" - <<PY
import os
from huggingface_hub import snapshot_download
kw = dict(repo_id="${HF_REPO}", local_dir="${HF_DIR}", resume_download=True)
tok = "${HF_TOKEN}"
if tok:
    kw["token"] = tok
snapshot_download(**kw)
print("[OK] Téléchargement terminé.")
PY
fi

# Vérif que la quantization NVFP4 est bien décrite dans le checkpoint
if [[ ! -f "${HF_DIR}/hf_quant_config.json" ]] && \
   ! grep -qi "fp4\|nvfp4\|quant" "${HF_DIR}/config.json" 2>/dev/null; then
  echo "[ATTENTION] Aucune trace de config quantization NVFP4 dans le checkpoint."
  echo "            Vérifie que le repo HF est bien la variante NVFP4."
fi

# ── 3. Options avancées (recommandées GB10) ───────────────────────────────────
echo ""
echo "[2.5/4] Téléchargement modèles embedding/reranker..."
bash "$(dirname "$0")/download_embeddings.sh"
echo "[3/4] Écriture des options avancées TRT-LLM..."
cat > "${PROJECT_ROOT}/models/hf_models/extra-llm-api-config.yml" <<'YAML'
# Options avancées TRT-LLM (backend PyTorch) — DGX Spark GB10 / NVFP4 MoE
# cuda graphs : accélère fortement la génération token/token
cuda_graph_config:
  enable_padding: true
  batch_sizes: [1, 2, 4, 8]
# MoE : backend adapté sm_121 (FP4 sur GB10 passe par des kernels dédiés)
moe_config:
  backend: CUTLASS
YAML
echo "[OK] -> models/hf_models/extra-llm-api-config.yml"

# ── 4. Warmup (compilation/caching des kernels sm_121) ───────────────────────
echo ""
echo "[4/4] Warmup : 1er lancement pour compiler/mettre en cache les kernels."
echo "      (peut prendre plusieurs minutes)"
echo ""
docker run --rm --gpus all --ipc=host \
  --ulimit memlock=-1 --ulimit stack=67108864 --shm-size=16g \
  -e CUTE_DSL_ARCH=sm_121a -e FLASHINFER_DISABLE_VERSION_CHECK=1 \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -v "${PROJECT_ROOT}/models/hf_models:/hf_models" \
  -p 127.0.0.1:8001:8000 \
  "${TRT_IMAGE}" \
  trtllm-serve "/hf_models/${MODEL_LOCAL_NAME}" \
    --backend pytorch \
    --host 0.0.0.0 --port 8000 \
    --max_batch_size 8 --max_num_tokens 16384 --max_seq_len 32768 \
    --kv_cache_free_gpu_memory_fraction 0.70 \
    --trust_remote_code \
    --extra_llm_api_options /hf_models/extra-llm-api-config.yml &

SERVE_PID=$!
echo "[INFO] Attente de la disponibilité (curl /v1/models)..."
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1; then
    echo ""
    echo "============================================================"
    echo "  [OK] Serveur prêt — kernels mis en cache."
    echo "  Test rapide :"
    echo "    curl http://127.0.0.1:8001/v1/models"
    echo "    curl http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \\"
    echo "      -d '{\"model\":\"qwen3-30b-a3b\",\"messages\":[{\"role\":\"user\",\"content\":\"2+2 ?\"}]}'"
    echo ""
    echo "  Arrêt du warmup puis démarrage complet :"
    echo "    docker compose -f docker-compose.yml -f docker-compose.gx10.yml \\"
    echo "      --profile gpu up -d"
    echo "============================================================"
    kill "${SERVE_PID}" 2>/dev/null || true
    exit 0
  fi
  sleep 15
done

echo "[ATTENTION] Timeout du warmup. Consulte les logs ci-dessus."
kill "${SERVE_PID}" 2>/dev/null || true
exit 1
