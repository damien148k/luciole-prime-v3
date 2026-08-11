# Sauvegarde et restauration d'une instance Luciole

Procédure complète pour sauvegarder une instance (ex. `mrae`) et la restaurer
sur la même machine ou sur une machine vierge.

## Ce qu'il faut sauvegarder

Une instance Luciole tient dans trois emplacements :

| Composant | Emplacement | Contenu |
|---|---|---|
| Dossier d'instance | `C:\RAG\luciole-<nom>\` | config (`settings.yaml`, `auth.yaml`), données (`data\`), feedbacks, modèles HF (`models\huggingface`), modèles Ollama (`models\ollama`), CA (`certs\ollama`) — tous les bind mounts |
| Volume Qdrant | `luciole-<nom>_qdrant_storage` | Index vectoriel (chunks + embeddings) |
| Volume OpenSearch | `luciole-<nom>_opensearch_data` | Index BM25 |

Les images Docker (`luciole-gpu`, `ollama`, `qdrant`, `opensearch`, `greenmail`)
sont re-téléchargeables, mais les inclure permet une restauration **hors-ligne**
sur une machine vierge.

## Sauvegarde automatisée (BACKUP.ps1)

```powershell
# Sauvegarde standard (instance + volumes, stack arrêtée puis redémarrée)
.\BACKUP.ps1 -InstanceName mrae

# Sauvegarde complète avec images Docker (pour machine vierge / offline)
.\BACKUP.ps1 -InstanceName mrae -IncludeImages -OutputDir "E:\sauvegardes"
```

Le script crée `mrae-<horodatage>\` contenant :

```
mrae-20260810-191500\
├── MANIFEST.json          # métadonnées (date, instance, contenu)
├── instance\              # copie de C:\RAG\luciole-mrae\
├── volumes\
│   ├── qdrant_storage.tar.gz
│   └── opensearch_data.tar.gz
└── images\                # (si -IncludeImages)
    ├── luciole-gpu.tar
    ├── ollama.tar
    ├── qdrant.tar
    ├── opensearch.tar
    └── greenmail.tar
```

La stack est arrêtée avant la copie (cohérence des index) puis redémarrée.
Utiliser `-KeepStackRunning` pour une sauvegarde à chaud (plus rapide, index
potentiellement incohérents — déconseillé avant une migration).

## Restauration (RESTORE.ps1)

### Sur la même machine (retour arrière)

```powershell
.\RESTORE.ps1 -BackupDir "D:\backups\luciole\mrae-20260810-191500"
```

### Sur une machine vierge

Prérequis : Docker Desktop installé et démarré (WSL2 activé).

```powershell
# 1. Copier le dossier de sauvegarde sur la machine (USB, réseau)
# 2. Restaurer avec les images Docker (pas besoin d'internet)
.\RESTORE.ps1 -BackupDir "E:\mrae-20260810-191500" -LoadImages
```

Le script :
1. Charge les images `.tar` (si `-LoadImages`)
2. Recrée `C:\RAG\luciole-mrae\`
3. Recrée et remplit les volumes `qdrant_storage` / `opensearch_data`
4. Démarre la stack et liste les conteneurs

Vérification post-restauration :

```powershell
docker exec luciole-ollama-mrae ollama list   # 3 modèles attendus
# Chat : http://localhost:8501
# Admin : http://localhost:8080
```

## Machine vierge derrière un proxy d'entreprise

Si la machine cible intercepte TLS (proxy/antivirus), le service Ollama a besoin
du CA racine. `INSTALL_OFFLINE.ps1` et `INSTALL.ps1` exportent désormais le
magasin Windows dans `certs\ollama\ca.crt` et écrivent `OLLAMA_CA_BUNDLE` dans
`.env` automatiquement. Si vous restaurez une sauvegarde qui contient déjà
`certs\ollama\ca.crt`, c'est réutilisé tel quel.

## Sauvegarde planifiée (optionnel)

Pour une sauvegarde quotidienne à 2 h du matin :

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\RAG\luciole-prime-v3\BACKUP.ps1`" -InstanceName mrae"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask -TaskName "Luciole-Backup-mrae" -Action $action -Trigger $trigger -RunLevel Highest
```
