#!/usr/bin/env python3
"""
setup_bge_model.py — Prépare le modèle BAAI/bge-m3 pour Luciole Prime V2

Problème : BAAI/bge-m3 ne distribue que pytorch_model.bin sur HuggingFace.
PyTorch < 2.6 bloque le chargement de .bin (CVE-2025-32434).
Ce script télécharge le .bin, le convertit en safetensors, puis supprime le .bin.

Usage (depuis le dossier du projet) :
    docker compose run --rm \\
      -e HF_HOME=/app/models/huggingface \\
      -e HF_HUB_OFFLINE=0 \\
      -e TRANSFORMERS_OFFLINE=0 \\
      -v "${PWD}/setup_bge_model.py:/setup_bge_model.py" \\
      agent python /setup_bge_model.py

Durée estimée : 3 à 5 minutes (téléchargement ~2.3 Go + conversion)
"""

from pathlib import Path
from huggingface_hub import hf_hub_download

# IMPORTANT : on ecrit dans le sous-dossier `hub/` car c'est l'emplacement
# standard du cache HuggingFace (HF_HOME/hub) et celui qu'embedder.py /
# reranker.py cherchent en premier (cf. _get_local_model_path).
HF_CACHE = "/app/models/huggingface/hub"
REPO_ID  = "BAAI/bge-m3"

# ─── Étape 1 : Télécharger tous les fichiers de config ────────────────────────
print("\n[1/4] Téléchargement des fichiers de configuration...")

config_files = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "1_Pooling/config.json",
    "sparse_linear.pt",
    "colbert_linear.pt",
]

for f in config_files:
    try:
        hf_hub_download(
            repo_id=REPO_ID,
            filename=f,
            cache_dir=HF_CACHE,
            local_files_only=False
        )
        print(f"  OK : {f}")
    except Exception as e:
        print(f"  IGNORÉ : {f} ({e})")

# ─── Étape 2 : Télécharger pytorch_model.bin ──────────────────────────────────
print("\n[2/4] Téléchargement de pytorch_model.bin (~2.3 Go)...")

bin_path = hf_hub_download(
    repo_id=REPO_ID,
    filename="pytorch_model.bin",
    cache_dir=HF_CACHE,
    local_files_only=False
)
print(f"  Téléchargé : {bin_path}")

# ─── Étape 3 : Convertir en safetensors ───────────────────────────────────────
print("\n[3/4] Conversion pytorch_model.bin → model.safetensors...")

import torch
from safetensors.torch import save_file


def _detect_bin_format(path):
    """Détecte le format d'un fichier .bin PyTorch à partir de ses premiers octets.

    Retourne un tuple (format, header) où format vaut "zip", "pickle" ou None.

    - b'\\x80'   : pickle legacy (torch.save « classique », PyTorch < 2.x)
    - b'PK\\x03\\x04' (alias b'PK') : conteneur ZIP (torch.save format PyTorch 2.x,
      utilisé par défaut par HuggingFace depuis 2024 pour BAAI/bge-m3)
    """
    with open(path, "rb") as fh:
        header = fh.read(4)
    if header[:2] == b"PK":
        return "zip", header
    if header[:1] == b"\x80":
        return "pickle", header
    return None, header


_fmt, _header = _detect_bin_format(bin_path)

if _fmt == "zip":
    # PyTorch 2.x sauvegarde ses .bin en conteneur ZIP. BAAI/bge-m3 est un modèle
    # officiel avec des wrappers SentenceTransformer : on ne peut pas restreindre
    # à weights_only=True (les classes SentenceTransformer ne sont pas whitelistées
    # par le unpickler restreint), donc weights_only=False est nécessaire et sûr ici.
    print("  Format détecté : ZIP (PyTorch 2.x)")
    state_dict = torch.load(bin_path, map_location="cpu", weights_only=False)
elif _fmt == "pickle":
    # Ancien format pickle legacy (torch.save avant PyTorch 2.x). Même remarque :
    # weights_only=True casserait le chargement des wrappers SentenceTransformer.
    print("  Format détecté : pickle legacy")
    state_dict = torch.load(bin_path, map_location="cpu", weights_only=False)
else:
    raise RuntimeError(
        f"Format inconnu du fichier {bin_path} — premiers bytes: {_header!r}"
    )

if "state_dict" in state_dict:
    state_dict = state_dict["state_dict"]

cache = Path(HF_CACHE) / "models--BAAI--bge-m3"
commit = (cache / "refs" / "main").read_text().strip()
snap   = cache / "snapshots" / commit
out    = snap / "model.safetensors"

save_file(state_dict, str(out))
print(f"  Sauvegardé : {out}")
print(f"  Taille     : {out.stat().st_size // 1024 // 1024} Mo")

# ─── Étape 4 : Supprimer pytorch_model.bin (évite erreur CVE au démarrage) ────
print("\n[4/4] Suppression de pytorch_model.bin...")

bin_in_snap = snap / "pytorch_model.bin"
if bin_in_snap.exists() or bin_in_snap.is_symlink():
    bin_in_snap.unlink()
    print(f"  Supprimé : {bin_in_snap}")

# Supprimer aussi le blob .bin s'il est présent en dur
# (on reconnaît un blob .bin PyTorch qu'il soit au format pickle legacy
#  ou au format ZIP PyTorch 2.x — cf. _detect_bin_format ci-dessus)
for b in (cache / "blobs").iterdir():
    with open(b, "rb") as f:
        header = f.read(4)
    if header[:1] == b'\x80' or header[:2] == b'PK':
        b.unlink()
        print(f"  Blob .bin supprimé : {b.name[:20]}...")

print("\n✅ BGE-M3 prêt. Vérifiez le health check :")
print("   (Invoke-WebRequest -Uri 'http://localhost:8000/api/health' -UseBasicParsing).Content")
