#!/usr/bin/env bash
# =============================================================================
# trt_entrypoint.gx10.sh — Entrypoint TensorRT-LLM pour DGX Spark GX10 (GB10)
#
# Backend PyTorch (TRT-LLM >= 1.2) : sert directement le checkpoint HF NVFP4,
# sans étape convert_checkpoint / trtllm-build.
# Kernels compilés/mis en cache au 1er run (warmup ~ quelques minutes).
#
# ⚠️  Convention CLI trtllm-serve 1.2.0rc2 :
#     - Options LLM : underscores  (--max_batch_size, --kv_cache_free_gpu_memory_fraction...)
#     - Options serveur : tirets   (--host, --port)
#     - --served_model_name N'EXISTE PAS dans cette version (le nom = dossier modèle)
#
# Monté via docker-compose.gx10.yml :
#   ./scripts/trt_entrypoint.gx10.sh -> /opt/entrypoint.sh
# =============================================================================

set -euo pipefail

MODEL_PATH="${MODEL_PATH:-/hf_models/Qwen3-30B-A3B-Instruct-2507-NVFP4}"
PORT="${TRITON_HTTP_PORT:-8000}"
MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-8}"
MAX_NUM_TOKENS="${MAX_NUM_TOKENS:-16384}"
MAX_SEQ_LEN="${MAX_SEQ_LEN:-32768}"
KV_FRAC="${KV_CACHE_FREE_GPU_MEM_FRACTION:-0.70}"

echo "============================================================"
echo "  [TRT-LLM 1.2 / GB10] trtllm-serve — backend PyTorch (NVFP4)"
echo "  Modèle         : ${MODEL_PATH}"
echo "  Port           : ${PORT}"
echo "  Max batch      : ${MAX_BATCH_SIZE}"
echo "  Max num tokens : ${MAX_NUM_TOKENS}"
echo "  Max seq len    : ${MAX_SEQ_LEN}"
echo "  KV cache frac  : ${KV_FRAC}  (mémoire UNIFIÉE — ne pas monter à 0.9)"
echo "  GPU arch       : ${CUTE_DSL_ARCH:-sm_121a}"
echo "============================================================"

# ── Vérification GPU ──────────────────────────────────────────────────────────
# nvidia-smi n'est pas disponible dans l'image trtllm/release — on vérifie
# uniquement que les capabilities GPU sont bien exposées via /dev/nvidia*
if ls /dev/nvidia* &>/dev/null; then
  echo "[INFO] GPU détecté via /dev/nvidia*"
else
  echo "[WARN] /dev/nvidia* absent — le GPU sera vérifié au démarrage de trtllm-serve."
fi

# ── Vérification du checkpoint HF ─────────────────────────────────────────────
if [[ ! -f "${MODEL_PATH}/config.json" ]]; then
  echo ""
  echo "============================================================"
  echo "  [ERREUR] Checkpoint HuggingFace introuvable : ${MODEL_PATH}"
  echo "  Télécharge-le d'abord SUR LE GX10 :"
  echo "    bash scripts/download_model.sh"
  echo "============================================================"
  exit 1
fi

# ── (Optionnel) options avancées via fichier YAML ─────────────────────────────
EXTRA_OPTS=()
if [[ -f "/hf_models/extra-llm-api-config.yml" ]]; then
  EXTRA_OPTS+=(--extra_llm_api_options /hf_models/extra-llm-api-config.yml)
  echo "[INFO] Options avancées : /hf_models/extra-llm-api-config.yml"
fi

# ── Lancement du serveur OpenAI-compatible (backend PyTorch) ──────────────────
# NB : trtllm-serve 1.2.0rc2 — pas de --served_model_name (option inexistante).
#      Le modèle est identifié par le chemin/dossier passé en premier argument.
exec trtllm-serve "${MODEL_PATH}" \
  --backend                           pytorch \
  --host                              0.0.0.0 \
  --port                              "${PORT}" \
  --max_batch_size                    "${MAX_BATCH_SIZE}" \
  --max_num_tokens                    "${MAX_NUM_TOKENS}" \
  --max_seq_len                       "${MAX_SEQ_LEN}" \
  --kv_cache_free_gpu_memory_fraction "${KV_FRAC}" \
  --trust_remote_code \
  "${EXTRA_OPTS[@]}"
