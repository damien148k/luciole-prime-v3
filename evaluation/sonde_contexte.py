#!/usr/bin/env python3
"""Mesure la fenetre de contexte effective du LLM, pas celle declaree.

Le code (src/generation/llm.py l.177-187) lit num_ctx dans settings.yaml
mais ne le transmet pas : la fenetre effective est celle du serveur —
defaut Ollama 4096 tokens, sauf OLLAMA_CONTEXT_LENGTH ou Modelfile.

Methode : on envoie des prompts de taille croissante a l'endpoint
/v1/chat/completions (celui que Luciole utilise reellement) et on lit
usage.prompt_tokens dans la reponse. Si le compte plafonne, le serveur
tronque. Un marqueur unique place en TETE du prompt confirme ce qui est
perdu : Ollama tronque par le debut, le marqueur disparait le premier.

Usage :
    docker exec luciole-agent-mrae python /app/evaluation/sonde_contexte.py

Aucune ecriture, aucun effet de bord : quatre appels en lecture seule.
"""
import json
import os
import urllib.request

BASE = os.environ.get("OLLAMA_URL", "http://ollama:11434")
if not BASE.endswith("/v1"):
    BASE = BASE + "/v1"
MODELE = os.environ.get("SONDE_MODELE", "qwen2.5:14b-instruct-q4_K_M")

MARQUEUR = "CODE-SECRET-7429"
# Un mot suivi d'un espace ~ 1-2 tokens. On vise large au-dessus de 4096.
TAILLES = [2000, 4000, 6000, 9000]


def appeler(messages, max_tokens=8):
    charge = {
        "model": MODELE,
        "messages": messages,
        "temperature": 0,
        "seed": 42,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(charge).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    print(f"Sonde de contexte — {BASE} — modele {MODELE}")
    print("-" * 70)
    plafond = None
    for cible in TAILLES:
        # Remplissage : mots distincts pour eviter une tokenisation
        # anormalement compacte d'une repetition exacte.
        mots = " ".join(f"mot{i}" for i in range(cible))
        prompt = f"{MARQUEUR} en tete. {mots}. Reponds simplement OK."
        res = appeler([{"role": "user", "content": prompt}])
        usage = res.get("usage", {}) or {}
        lus = usage.get("prompt_tokens", -1)
        envoyes_estime = cible * 2  # borne haute grossiere
        tronque = lus < cible  # si le serveur a vu moins que le nombre
                               # de mots, il a forcement tronque
        print(f"  cible ~{cible:5} mots -> prompt_tokens={lus:6} "
              f"{'TRONQUE' if tronque else 'ok'}")
        if tronque and plafond is None:
            plafond = lus

    print("-" * 70)
    if plafond:
        print(f"Plafond effectif : ~{plafond} tokens (le serveur tronque).")
    else:
        print(f"Aucune troncature jusqu'a {TAILLES[-1]} mots "
              f"(~{TAILLES[-1] * 2} tokens).")

    # Test fonctionnel : le marqueur de tete survit-il a un prompt long ?
    mots = " ".join(f"mot{i}" for i in range(9000))
    prompt = (f"Le code secret est {MARQUEUR}. {mots}. "
              f"Quel est le code secret ? Reponds par le code seul.")
    res = appeler([{"role": "user", "content": prompt}], max_tokens=16)
    texte = res.get("choices", [{}])[0].get("message", {}).get("content", "")
    retrouve = MARQUEUR in texte
    print(f"Marqueur en tete d'un prompt de 9000 mots : "
          f"{'RETROUVE' if retrouve else 'PERDU — la tete du prompt est tronquee'}")
    print(f"  (prompt_tokens={res.get('usage', {}).get('prompt_tokens', -1)}, "
          f"reponse: {texte.strip()[:60]!r})")

    print("-" * 70)
    print("Lecture : un prompt RAG de 25 passages x ~1000 caracteres")
    print("represente 8000-12000 tokens. Si le plafond est 4096, les")
    print("premiers passages du prompt n'atteignent jamais le modele.")
    print("Correctif cote serveur : OLLAMA_CONTEXT_LENGTH=8192 dans")
    print("l'environnement du service ollama, puis redemarrage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
