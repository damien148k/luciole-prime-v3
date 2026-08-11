#Requires -Version 5.1
<#
.SYNOPSIS
  Restaure une instance Luciole depuis une sauvegarde creee par BACKUP.ps1.

.DESCRIPTION
  Restaure dans l'ordre :
    1. (optionnel) les images Docker depuis images\*.tar (-LoadImages)
    2. le dossier d'instance vers C:\RAG\luciole-<nom>\
    3. les volumes nommes (qdrant_storage, opensearch_data)
    4. demarre la stack et verifie la sante

  Compatible Constrained Language Mode (AppLocker/WDAC) : cmdlets + docker
  uniquement, aucun appel de methode .NET.

.PARAMETER BackupDir
  Dossier de sauvegarde (contient MANIFEST.json, instance\, volumes\).

.PARAMETER InstanceName
  Nom d'instance cible. Defaut : lu depuis MANIFEST.json.

.PARAMETER LoadImages
  Charge les images Docker depuis images\*.tar (machine vierge / offline).

.PARAMETER TargetRoot
  Racine d'installation. Defaut : C:\RAG.

.EXAMPLE
  .\RESTORE.ps1 -BackupDir "D:\backups\luciole\mrae-20260810-191500"
  .\RESTORE.ps1 -BackupDir "E:\save\mrae-20260810-191500" -LoadImages
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,

    [string]$InstanceName = "",
    [switch]$LoadImages,
    [string]$TargetRoot = "C:\RAG"
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Msg) Write-Host ""; Write-Host ">> $Msg" -ForegroundColor Cyan; Write-Host ("-" * 60) }
function Write-OK   { param([string]$Msg) Write-Host "  [OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "  [!] $Msg" -ForegroundColor Yellow }

if (-not (Test-Path "$BackupDir\MANIFEST.json")) {
    throw "MANIFEST.json introuvable dans $BackupDir -- ce n'est pas une sauvegarde BACKUP.ps1"
}
$manifest = Get-Content "$BackupDir\MANIFEST.json" -Raw | ConvertFrom-Json

if (-not $InstanceName) { $InstanceName = $manifest.instance }
$InstancePath = Join-Path $TargetRoot "luciole-$InstanceName"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Yellow
Write-Host "  Restauration Luciole -- $InstanceName" -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Yellow
Write-Host "  Sauvegarde : $BackupDir (creee le $($manifest.created))"
Write-Host "  Cible      : $InstancePath"
Write-Host ""

# Verification Docker
try {
    docker ps 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop n'est pas demarre" }
    Write-OK "Docker actif"
} catch {
    Write-Host "  [ERREUR] $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "Appuyez sur Entree pour quitter"; exit 1
}

# ----------------------------------------------------------------------------
# 1. (Optionnel) Charger les images Docker
# ----------------------------------------------------------------------------
if ($LoadImages -and (Test-Path "$BackupDir\images")) {
    Write-Step "1/4 -- Chargement des images Docker"
    Get-ChildItem -Path "$BackupDir\images" -Filter "*.tar" | ForEach-Object {
        Write-Host "  Chargement $($_.Name)..."
        docker load -i $_.FullName
        if ($LASTEXITCODE -eq 0) { Write-OK $_.Name } else { Write-Warn "Echec $($_.Name)" }
    }
} else {
    Write-Step "1/4 -- Images Docker (ignorees, -LoadImages non fourni)"
}

# ----------------------------------------------------------------------------
# 2. Restaurer le dossier d'instance
# ----------------------------------------------------------------------------
Write-Step "2/4 -- Restauration de l'instance"
if (Test-Path $InstancePath) {
    Write-Warn "L'instance existe deja : $InstancePath"
    $confirm = Read-Host "  Ecraser ? (oui/non)"
    if ($confirm -ne "oui") { Write-Host "  Annule." -ForegroundColor Yellow; exit 0 }
    $oldProfile = "gpu"
    if (Test-Path "$InstancePath\.env") {
        $om = Select-String -Path "$InstancePath\.env" -Pattern '^COMPOSE_PROFILES=(\w+)' | Select-Object -First 1
        if ($om) { $oldProfile = $om.Matches[0].Groups[1].Value }
    }
    Push-Location $InstancePath
    $ErrorActionPreference = "Continue"
    docker compose --profile $oldProfile down 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Pop-Location
}
New-Item -ItemType Directory -Force -Path $InstancePath | Out-Null
Write-Host "  Copie des fichiers (peut prendre plusieurs minutes)..."
cmd /c "xcopy `"$BackupDir\instance`" `"$InstancePath`" /E /H /Y /Q" | Out-Null
if ($LASTEXITCODE -eq 0) { Write-OK "Instance restauree" } else { Write-Warn "xcopy code $LASTEXITCODE" }

# ----------------------------------------------------------------------------
# 3. Restaurer les volumes nommes
# ----------------------------------------------------------------------------
Write-Step "3/4 -- Restauration des volumes Docker nommes"
$projectName = "luciole-$InstanceName"
foreach ($vol in $manifest.volumes) {
    $archive = Join-Path "$BackupDir\volumes" "$vol.tar.gz"
    if (-not (Test-Path $archive)) { Write-Warn "$vol.tar.gz absent (ignore)"; continue }
    $fullVol = "${projectName}_${vol}"

    $ErrorActionPreference = "Continue"
    docker volume rm $fullVol 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    docker volume create $fullVol | Out-Null

    Write-Host "  Restauration $fullVol..."
    # ollama/ollama (Ubuntu, contient tar+gzip) est toujours chargee par
    # INSTALL_OFFLINE -- contrairement a alpine, absente d'une machine offline.
    docker run --rm -v "${fullVol}:/data" -v "${BackupDir}\volumes:/backup:ro" ollama/ollama:latest tar xzf "/backup/${vol}.tar.gz" -C /data
    if ($LASTEXITCODE -eq 0) { Write-OK $vol } else { Write-Warn "Echec restauration $vol" }
}

# ----------------------------------------------------------------------------
# 4. Demarrage + verification
# ----------------------------------------------------------------------------
Write-Step "4/4 -- Demarrage de la stack"
# Profil lu depuis le .env restaure (defaut gpu)
$Profile = "gpu"
if (Test-Path "$InstancePath\.env") {
    $m = Select-String -Path "$InstancePath\.env" -Pattern '^COMPOSE_PROFILES=(\w+)' | Select-Object -First 1
    if ($m) { $Profile = $m.Matches[0].Groups[1].Value }
}
Push-Location $InstancePath
docker compose --profile $Profile up -d
Pop-Location
Write-Host "  Attente stabilisation (30 s)..."
Start-Sleep -Seconds 30

Write-Host ""
Write-Host "  Conteneurs :" -ForegroundColor White
docker ps --filter "name=luciole-" --format "    {{.Names}}  ({{.Status}})"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Restauration terminee" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Instance : $InstancePath"
Write-Host ""
# Ports reels lus depuis le .env restaure
$chatPort = "8501"; $adminPort = "8080"
if (Test-Path "$InstancePath\.env") {
    $cp = Select-String -Path "$InstancePath\.env" -Pattern '^CHAT_PORT=(\d+)' | Select-Object -First 1
    if ($cp) { $chatPort = $cp.Matches[0].Groups[1].Value }
    $ap = Select-String -Path "$InstancePath\.env" -Pattern '^ADMIN_PORT=(\d+)' | Select-Object -First 1
    if ($ap) { $adminPort = $ap.Matches[0].Groups[1].Value }
}
Write-Host "  Verifiez :" -ForegroundColor Yellow
Write-Host "    - Chat   : http://localhost:$chatPort"
Write-Host "    - Admin  : http://localhost:$adminPort"
Write-Host "    - Ollama : docker exec luciole-ollama-$InstanceName ollama list"
Write-Host ""
