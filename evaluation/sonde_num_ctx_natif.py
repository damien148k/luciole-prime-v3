#!/usr/bin/env python3
"""
Sonde num_ctx natif — vérifie que l'API native d'Ollama (/api/chat) honore
options.num_ctx A LA REQUETE, sur la version du serveur en production.

Pourquoi cette sonde existe : l'endpoint compatible OpenAI d'Ollama ignore
num_ctx (sonde_contexte.py, mesure du 2026-08-30 : 2048 tokens effectifs
malgre num_ctx=16384 dans settings.yaml). Le chemin api_format: ollama de
llm.py passe par l'API native, qui est censee accepter options.num_ctx —
mais ce comportement depend de la version du serveur. Cette sonde le
prouve avant que settings.yaml ne s'y fie.

Protocole : le MEME prompt de ~4000 mots est envoye deux fois, une fois
avec num_ctx=2048, une fois avec num_ctx=8192. Si prompt_eval_count suit
la valeur demandee (~2000 puis ~4100), le pilotage par requete fonctionne.
Si les deux plafonnent a ~2000, le serveur est trop ancien : mettre a jour
l'image ollama ou fixer OLLAMA_CONTEXT_LENGTH cote service.

Aucun effet de bord : deux generations de quelques tokens, aucune ecriture.

Usage :
    docker exec luciole-agent-mrae python /app/evaluation/sonde_num_ctx_natif.py

Variables :
    OLLAMA_URL    defaut http://ollama:11434
    SONDE_MODELE  defaut qwen2.5:14b-instruct-q4_K_M
"""

import json
import os
import urllib.request

BASE = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MODELE = os.environ.get("SONDE_MODELE", "qwen2.5:14b-instruct-q4_K_M")
MOTS_CIBLE = 4000


def version_serveur() -> str:
    try:
        with urllib.request.urlopen(f"{BASE}/api/version", timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("version", "?")
    except Exception:
        return "injoignable"


def appel_natif(prompt: str, num_ctx: int) -> int:
    """POST /api/chat avec options.num_ctx ; retourne prompt_eval_count."""
    corps = {
        "model": MODELE,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": num_ctx, "num_predict": 8, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps(corps).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8")).get("prompt_eval_count", 0)


def fabriquer_prompt(nb_mots: int) -> str:
    """~4000 mots uniques, termines par une consigne de fin."""
    mots = " ".join(f"mot{i}" for i in range(nb_mots))
    return (
        f"{mots}\n\nIgnore tout ce qui precede. Reponds uniquement : OK."
    )


def main() -> None:
    print(f"Sonde num_ctx natif — {BASE} — modele {MODELE}")
    print(f"Version du serveur Ollama : {version_serveur()}")
    print("-" * 70)
    prompt = fabriquer_prompt(MOTS_CIBLE)
    attendu_haut = int(MOTS_CIBLE * 1.1)  # tokens >= mots ; marge empirique
    resultats = {}
    for num_ctx in (2048, 8192):
        tokens = appel_natif(prompt, num_ctx)
        resultats[num_ctx] = tokens
        verdict = "ok" if (num_ctx == 2048) != (tokens > 3000) else "?"
        print(f"  num_ctx={num_ctx:>5} -> prompt_eval_count={tokens:>5}  {verdict}")
    print("-" * 70)
    bas, haut = resultats[2048], resultats[8192]
    if haut > 3000 > bas:
        print(
            "VERDICT : le pilotage par requete fonctionne — api_format: ollama\n"
            "  + num_ctx dans settings.yaml controlent la fenetre (reload-config\n"
            "  applique sans rebuild, llm_generator etant recree au rechargement)."
        )
    elif bas > 3000:
        print(
            "VERDICT : meme num_ctx=2048 depasse 3000 tokens — le serveur ne\n"
            "  borne pas a la valeur demandee. Etrange mais sans gravité pour\n"
            "  l'usage : fixer num_ctx a la valeur voulue et s'y tenir."
        )
    else:
        print(
            "VERDICT : num_ctx est ignore meme sur l'API native — serveur trop\n"
            "  ancien. Correctif : mettre a jour l'image ollama, ou fixer\n"
            "  OLLAMA_CONTEXT_LENGTH=32768 dans l'environnement du service\n"
            "  ollama puis recreer le conteneur, et repasser sonde_contexte.py."
        )


if __name__ == "__main__":
    main()
