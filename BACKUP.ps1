#Requires -Version 5.1
<#
.SYNOPSIS
  Sauvegarde complete d'une instance Luciole (config, donnees, index, modeles).

.DESCRIPTION
  Cree un dossier de sauvegarde horodate contenant :
    - instance\   : copie complete de C:\RAG\luciole-<nom>\ (bind mounts)
    - volumes\    : archives tar.gz des volumes nommes (Qdrant, OpenSearch)
    - images\     : (optionnel) images Docker en .tar (-IncludeImages)
    - MANIFEST.json : metadonnees (date, instance, contenu)

  Compatible Constrained Language Mode (AppLocker/WDAC) : cmdlets + docker
  uniquement, aucun appel de methode .NET.

.PARAMETER InstanceName
  Nom de l'instance (ex: mrae). Defaut : mrae.

.PARAMETER OutputDir
  Racine des sauvegardes. Defaut : D:\backups\luciole.

.PARAMETER IncludeImages
  Exporte aussi les images Docker (luciole-gpu, ollama, qdrant, opensearch,
  greenmail) en .tar. Ajoute plusieurs Go mais permet une restauration
  sur machine vierge sans internet.

.PARAMETER KeepStackRunning
  Ne pas arreter la stack avant la sauvegarde (moins coherent, plus rapide).

.EXAMPLE
  .\BACKUP.ps1 -InstanceName mrae
  .\BACKUP.ps1 -InstanceName mrae -IncludeImages -OutputDir "E:\save"
#>
param(
    [string]$InstanceName = "mrae",
    [string]$OutputDir = "D:\backups\luciole",
    [switch]$IncludeImages,
    [switch]$KeepStackRunning
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Msg) Write-Host ""; Write-Host ">> $Msg" -ForegroundColor Cyan; Write-Host ("-" * 60) }
function Write-OK   { param([string]$Msg) Write-Host "  [OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "  [!] $Msg" -ForegroundColor Yellow }

$InstancePath = "C:\RAG\luciole-$InstanceName"
if (-not (Test-Path $InstancePath)) {
    throw "Instance introuvable : $InstancePath"
}

# Profil lu depuis le .env de l'instance (defaut gpu)
$Profile = "gpu"
if (Test-Path "$InstancePath\.env") {
    $m = Select-String -Path "$InstancePath\.env" -Pattern '^COMPOSE_PROFILES=(\w+)' | Select-Object -First 1
    if ($m) { $Profile = $m.Matches[0].Groups[1].Value }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $OutputDir "$InstanceName-$timestamp"
New-Item -ItemType Directory -Force -Path "$backupDir\instance", "$backupDir\volumes" | Out-Null

Write-Host ""
Write-Host "================================================================" -ForegroundColor Yellow
Write-Host "  Sauvegarde Luciole -- $InstanceName" -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Yellow
Write-Host "  Source : $InstancePath"
Write-Host "  Cible  : $backupDir"
Write-Host ""

# ----------------------------------------------------------------------------
# 1. Arret de la stack (coherence des index Qdrant/OpenSearch)
# ----------------------------------------------------------------------------
if (-not $KeepStackRunning) {
    Write-Step "1/5 -- Arret de la stack (flush des index)"
    Push-Location $InstancePath
    $ErrorActionPreference = "Continue"
    docker compose --profile $Profile down 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Pop-Location
    Write-OK "Stack arretee"
} else {
    Write-Warn "Stack laissee en marche : les index peuvent etre incoherents"
}

# ----------------------------------------------------------------------------
# 2. Copie du dossier d'instance (bind mounts : config, data, models, certs)
# ----------------------------------------------------------------------------
Write-Step "2/5 -- Copie de l'instance (config, data, models, certs)"
Write-Host "  (peut prendre plusieurs minutes selon la taille des modeles)..."
cmd /c "xcopy `"$InstancePath`" `"$backupDir\instance`" /E /H /Y /Q" | Out-Null
if ($LASTEXITCODE -eq 0) { Write-OK "Instance copiee" } else { Write-Warn "xcopy code $LASTEXITCODE (partiel possible)" }

# ----------------------------------------------------------------------------
# 3. Export des volumes nommes (Qdrant + OpenSearch)
# ----------------------------------------------------------------------------
Write-Step "3/5 -- Export des volumes Docker nommes"
$projectName = "luciole-$InstanceName"
$namedVolumes = @("qdrant_storage", "opensearch_data")
foreach ($vol in $namedVolumes) {
    $fullVol = "${projectName}_${vol}"
    $ErrorActionPreference = "Continue"
    $exists = docker volume inspect $fullVol 2>&1
    $ErrorActionPreference = "Stop"
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Volume $fullVol introuvable (ignore)"
        continue
    }
    Write-Host "  Export $fullVol..."
    # ollama/ollama (Ubuntu, contient tar+gzip) est toujours presente dans une
    # installation Luciole -- contrairement a alpine, absente d'une machine offline.
    docker run --rm -v "${fullVol}:/data:ro" -v "${backupDir}\volumes:/backup" ollama/ollama:latest tar czf "/backup/${vol}.tar.gz" -C /data .
    if ($LASTEXITCODE -eq 0) { Write-OK "$vol.tar.gz" } else { Write-Warn "Echec export $vol" }
}

# ----------------------------------------------------------------------------
# 4. (Optionnel) Export des images Docker
# ----------------------------------------------------------------------------
if ($IncludeImages) {
    Write-Step "4/5 -- Export des images Docker (.tar)"
    $images = @(
        @{ Name = "luciole-gpu:latest";                    File = "luciole-gpu.tar" },
        @{ Name = "ollama/ollama:latest";                  File = "ollama.tar" },
        @{ Name = "qdrant/qdrant:v1.7.4";                  File = "qdrant.tar" },
        @{ Name = "opensearchproject/opensearch:2.11.0";   File = "opensearch.tar" },
        @{ Name = "greenmail/standalone:latest";           File = "greenmail.tar" }
    )
    New-Item -ItemType Directory -Force -Path "$backupDir\images" | Out-Null
    foreach ($img in $images) {
        $ErrorActionPreference = "Continue"
        $present = docker images --format "{{.Repository}}:{{.Tag}}" 2>$null | Select-String -SimpleMatch $img.Name
        $ErrorActionPreference = "Stop"
        if (-not $present) { Write-Warn "$($img.Name) absente (ignoree)"; continue }
        Write-Host "  Export $($img.Name)..."
        docker save -o (Join-Path "$backupDir\images" $img.File) $img.Name
        if ($LASTEXITCODE -eq 0) { Write-OK $img.File } else { Write-Warn "Echec $($img.File)" }
    }
} else {
    Write-Step "4/5 -- Images Docker ignorees (utiliser -IncludeImages pour les exporter)"
}

# ----------------------------------------------------------------------------
# 5. Manifeste + redemarrage
# ----------------------------------------------------------------------------
Write-Step "5/5 -- Manifeste et redemarrage"
$manifest = @{
    instance    = $InstanceName
    created     = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    source_path = $InstancePath
    volumes     = $namedVolumes
    images      = [bool]$IncludeImages
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content -Path "$backupDir\MANIFEST.json" -Encoding UTF8
Write-OK "MANIFEST.json"

if (-not $KeepStackRunning) {
    Write-Host "  Redemarrage de la stack..."
    Push-Location $InstancePath
    $ErrorActionPreference = "Continue"
    docker compose --profile $Profile up -d 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Pop-Location
    Write-OK "Stack redemarree"
}

$sizeBytes = 0
Get-ChildItem -Path $backupDir -Recurse -File | ForEach-Object { $sizeBytes += $_.Length }
$sizeGB = [int](($sizeBytes / 1GB) * 100 + 0.5) / 100

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Sauvegarde terminee" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Dossier : $backupDir"
Write-Host "  Taille  : $sizeGB Go"
Write-Host ""
Write-Host "  Restauration : .\RESTORE.ps1 -BackupDir `"$backupDir`"" -ForegroundColor Cyan
Write-Host ""
