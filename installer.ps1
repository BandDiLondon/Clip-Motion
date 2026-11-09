<#
Installation de Clip Motion (Windows 10/11, carte AMD) : ComfyUI portable AMD, nœud ComfyUI-GGUF, modèles Wan 2.2.
Lancé par Installer.bat (double-clic). Relançable : ne refait que ce qui manque, reprend un téléchargement coupé.
Chaque fichier téléchargé est vérifié par sa taille et son empreinte SHA256 avant d'être utilisé.

  Installer.bat                  installe ce qui manque (demande confirmation avant de télécharger)
  Installer.bat -Oui             sans question (pour un assistant IA, APRÈS accord de l'utilisateur)
  Installer.bat -Verifier        vérifie aussi l'empreinte SHA256 des modèles déjà présents (~1 à 3 min)
  Installer.bat -SansModeles     installe ComfyUI et le nœud, pas les modèles (~29 Go)
#>
param([switch]$Oui, [switch]$Verifier, [switch]$SansModeles, [switch]$Bibliotheque)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Racine = $PSScriptRoot
$Comfy = Join-Path $Racine 'comfyui\ComfyUI_windows_portable'
$Py = Join-Path $Comfy 'python_embeded\python.exe'
$Modeles = Join-Path $Comfy 'ComfyUI\models'
$Noeud = Join-Path $Comfy 'ComfyUI\custom_nodes\ComfyUI-GGUF'

# Versions testées (2026-11-09). Changer une version = mettre à jour taille et empreinte.
$ComfyUI = [pscustomobject]@{
    Url    = 'https://github.com/Comfy-Org/ComfyUI/releases/download/v0.44.0/ComfyUI_windows_portable_amd.7z'
    Taille = 1671700941
    Sha    = 'b9ff564ced02a9a9f5dd3b6a5228ee2f1da33303a4f34f5438b8b80428fedd28'
}
$GgufCommit = '6ea2651e7df66d7585f6ffee804b20e92fb38b8a'
$GgufPip = 'gguf==0.19.0'
$HF = 'https://huggingface.co'
$Fichiers = @(
    [pscustomobject]@{ Dossier = 'unet'; Nom = 'Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf'; Taille = 9651728896
        Url = "$HF/QuantStack/Wan2.2-I2V-A14B-GGUF/resolve/main/HighNoise/Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf"
        Sha = '836250abfaa3411694e2c9cf3a0cc18265329d5156d81aa116d5366b0f8f02e7' }
    [pscustomobject]@{ Dossier = 'unet'; Nom = 'Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf'; Taille = 9651728896
        Url = "$HF/QuantStack/Wan2.2-I2V-A14B-GGUF/resolve/main/LowNoise/Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf"
        Sha = 'e2f98d834af009d035c6b0918268f2eba0aa8a63025ce942277e2384d40b0866' }
    [pscustomobject]@{ Dossier = 'loras'; Nom = 'wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors'; Taille = 1226977424
        Url = "$HF/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors"
        Sha = 'd176c808d6fc461999b68e321efcb7501b20b8c3797523ed0df14f7d1deff11e' }
    [pscustomobject]@{ Dossier = 'loras'; Nom = 'wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors'; Taille = 1226977424
        Url = "$HF/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"
        Sha = '024f21de095bc8fad9809ded3e9e49a2e170dcf27075da8145ba7d60d8aab7f9' }
    [pscustomobject]@{ Dossier = 'text_encoders'; Nom = 'umt5_xxl_fp8_e4m3fn_scaled.safetensors'; Taille = 6735906897
        Url = "$HF/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        Sha = 'c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68' }
    [pscustomobject]@{ Dossier = 'vae'; Nom = 'wan_2.1_vae.safetensors'; Taille = 253815318
        Url = "$HF/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors"
        Sha = '2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b' }
)

function Etape($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "  OK      $t" -ForegroundColor Green }
function Avert($t) { Write-Host "  ATTENTION  $t" -ForegroundColor Yellow }
function Echec($t) { Write-Host "`n  ECHEC   $t" -ForegroundColor Red; exit 1 }
function Go($octets) { if ($octets -lt 1GB) { '{0:N0} Mo' -f ($octets / 1MB) } else { '{0:N1} Go' -f ($octets / 1GB) } }
function SansArret([scriptblock]$Bloc) {
    # PowerShell 5.1 : rediriger la sortie d'erreur d'un programme externe le ferait échouer en mode Stop
    $avant = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Bloc } finally { $ErrorActionPreference = $avant }
}
function Empreinte($f) { (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLower() }

function Telecharger($Url, $Dest, $Taille, $Sha) {
    <# Télécharge dans <dest>.part (reprise si coupé), vérifie taille + SHA256, puis renomme. #>
    $part = "$Dest.part"
    New-Item -ItemType Directory -Force (Split-Path $Dest) | Out-Null
    $deja = if (Test-Path -LiteralPath $part) { (Get-Item -LiteralPath $part).Length } else { 0 }
    if ($deja -gt $Taille) { Remove-Item -LiteralPath $part; $deja = 0 }
    if ($deja -lt $Taille) {
        Write-Host "  téléchargement de $(Split-Path $Dest -Leaf) ($(Go $Taille))$(if ($deja) { ', reprise à ' + (Go $deja) })"
        & curl.exe -L --fail --retry 5 --retry-delay 5 -C - --progress-bar -o $part $Url
        if ($LASTEXITCODE -ne 0) { Echec "téléchargement interrompu (code $LASTEXITCODE). Relance l'installateur : il reprendra où il s'est arrêté.`n          $Url" }
    }
    $taille_lue = (Get-Item -LiteralPath $part).Length
    if ($taille_lue -ne $Taille) { Echec "taille inattendue pour $(Split-Path $Dest -Leaf) : $taille_lue octets au lieu de $Taille. Relance l'installateur." }
    Write-Host '  vérification de l''empreinte SHA256…'
    if ((Empreinte $part) -ne $Sha) {
        Remove-Item -LiteralPath $part
        Echec "empreinte SHA256 incorrecte pour $(Split-Path $Dest -Leaf) : fichier supprimé. Relance l'installateur."
    }
    Move-Item -Force -LiteralPath $part -Destination $Dest
    Ok (Split-Path $Dest -Leaf)
}

function Decompresser7z($Archive, $Dossier) {
    $septzip = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $septzip) { $septzip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source }
    if ($septzip) {
        & $septzip x -y "-o$Dossier" $Archive | Out-Null
    } else {
        # tar de Windows (libarchive) : lit le 7z sous Windows 11 récent
        & "$env:SystemRoot\System32\tar.exe" -xf $Archive -C $Dossier
    }
    if ($LASTEXITCODE -ne 0) {
        Echec "décompression impossible. Installe 7-Zip (https://www.7-zip.org) puis relance l'installateur."
    }
}

if ($Bibliotheque) { return }  # chargé par les tests : fonctions seulement

Write-Host '=============================================='
Write-Host '  Installation de Clip Motion (Windows + AMD)'
Write-Host '=============================================='

# ---------- 1. Vérifications du poste (ne bloquent que si c'est indispensable)
Etape 'Poste'
if ($env:OS -ne 'Windows_NT') { Echec 'Clip Motion ne fonctionne que sous Windows 10/11.' }
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { Echec 'curl.exe introuvable (fourni avec Windows 10 1803 et plus récent).' }
if ($Racine.Length -gt 120) { Avert "chemin long ($($Racine.Length) caractères) : en cas d'erreur, déplace le dossier plus près de la racine, ex. C:\Clip-Motion." }
$cartes = (Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ', '
if ($cartes -match 'Radeon|AMD') { Ok "carte graphique : $cartes" } else { Avert "pas de carte AMD Radeon détectée ($cartes) : Clip Motion n'est testé que sur AMD (ROCm)." }
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Avert 'ffmpeg / ffprobe absents du PATH : nécessaires pour le post-traitement (https://www.gyan.dev/ffmpeg/builds/ ou winget install Gyan.FFmpeg).'
} elseif (-not ((SansArret { & ffmpeg -hide_banner -encoders 2>$null }) -match 'h264_amf')) {
    Avert 'ffmpeg sans encodeur h264_amf (AMD) : le post-traitement échouera.'
} else { Ok 'ffmpeg avec h264_amf' }

# ---------- 2. Ce qui manque
$aFaire = @()
$total = 0
if (-not (Test-Path $Py)) { $aFaire += "ComfyUI portable AMD v0.44.0 ($(Go $ComfyUI.Taille) à télécharger, ~8 Go une fois décompressé)"; $total += $ComfyUI.Taille }
if (-not (Test-Path (Join-Path $Noeud '__init__.py'))) { $aFaire += 'nœud ComfyUI-GGUF (< 1 Mo)' }
$modelesManquants = @()
if (-not $SansModeles) {
    foreach ($f in $Fichiers) {
        $dest = Join-Path $Modeles "$($f.Dossier)\$($f.Nom)"
        if (-not (Test-Path -LiteralPath $dest) -or (Get-Item -LiteralPath $dest).Length -ne $f.Taille) {
            $modelesManquants += $f
            $aFaire += "$($f.Nom) ($(Go $f.Taille))"
            $total += $f.Taille
        }
    }
}
Etape 'À installer'
if (-not $aFaire) {
    Ok 'tout est déjà installé'
} else {
    $aFaire | ForEach-Object { Write-Host "  - $_" }
    $lecteur = Get-PSDrive -Name $Racine.Substring(0, 1)
    Write-Host "  Total à télécharger : $(Go $total) ; libre sur $($lecteur.Name): : $(Go $lecteur.Free)"
    if ($lecteur.Free -lt $total + 10GB) { Echec "pas assez de place sur le disque $($lecteur.Name): (il faut $(Go ($total + 10GB)) avec la marge de décompression)." }
    if (-not $Oui) {
        $r = Read-Host '  Continuer ? (o/n)'
        if ($r -notmatch '^[oOyY]') { Write-Host '  Annulé : rien n''a été téléchargé.'; exit 0 }
    }
}

# ---------- 3. ComfyUI portable AMD
if (-not (Test-Path $Py)) {
    Etape 'ComfyUI portable AMD'
    $dossierComfy = Join-Path $Racine 'comfyui'
    $archive = Join-Path $dossierComfy 'ComfyUI_windows_portable_amd.7z'
    Telecharger $ComfyUI.Url $archive $ComfyUI.Taille $ComfyUI.Sha
    Write-Host '  décompression (quelques minutes)…'
    Decompresser7z $archive $dossierComfy
    if (-not (Test-Path $Py)) { Echec "décompression incomplète : $Py introuvable." }
    Remove-Item -LiteralPath $archive
    Ok 'ComfyUI décompressé'
}

# ---------- 4. Nœud ComfyUI-GGUF (version figée) + paquet gguf
if (-not (Test-Path (Join-Path $Noeud '__init__.py'))) {
    Etape 'Nœud ComfyUI-GGUF'
    $tmp = Join-Path $env:TEMP "clipmotion_gguf_$PID"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    & curl.exe -L --fail --retry 3 -s -o "$tmp\gguf.zip" "https://codeload.github.com/city96/ComfyUI-GGUF/zip/$GgufCommit"
    if ($LASTEXITCODE -ne 0) { Echec 'téléchargement de ComfyUI-GGUF impossible (connexion ?).' }
    Expand-Archive -Force "$tmp\gguf.zip" $tmp
    Move-Item -Force (Join-Path $tmp "ComfyUI-GGUF-$GgufCommit") $Noeud
    Remove-Item -Recurse -Force $tmp
    Ok "ComfyUI-GGUF ($($GgufCommit.Substring(0, 7)))"
}
SansArret { & $Py -s -c 'import gguf' 2>$null }
if ($LASTEXITCODE -ne 0) {
    Etape 'Paquet Python gguf'
    & $Py -s -m pip install --no-cache-dir $GgufPip
    if ($LASTEXITCODE -ne 0) { Echec "installation de $GgufPip impossible." }
    Ok $GgufPip
}

# ---------- 5. Modèles
if (-not $SansModeles) {
    Etape 'Modèles Wan 2.2'
    foreach ($f in $Fichiers) {
        $dest = Join-Path $Modeles "$($f.Dossier)\$($f.Nom)"
        if ($modelesManquants -contains $f) {
            Telecharger $f.Url $dest $f.Taille $f.Sha
        } elseif ($Verifier) {
            Write-Host "  vérification de $($f.Nom)…"
            if ((Empreinte $dest) -ne $f.Sha) { Echec "empreinte incorrecte pour $dest : supprime ce fichier et relance l'installateur." }
            Ok "$($f.Nom) (empreinte vérifiée)"
        } else {
            Ok "$($f.Nom) (déjà présent)"
        }
    }
}

# ---------- 6. Vérification finale
Etape 'Vérification'
SansArret { & $Py -s (Join-Path $Racine 'outils\test_clipmotion.py') 2>&1 } | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } |
    Select-Object -Last 2 | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { Echec 'les tests de Clip Motion échouent : voir le message ci-dessus.' }
Ok 'tests de Clip Motion'
Write-Host "`nInstallation terminée. Double-clique sur « Clip Motion.bat » pour ouvrir l'interface." -ForegroundColor Green
