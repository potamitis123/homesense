<#
    One-time setup for the ESP32-P4 intercom port.

    Does the two things that have to happen before this project can build:

      1. Copies the application sources across from the working ESP32-S3-BOX-3
         project (../main). They are copied rather than rewritten so the
         verified audio/MQTT/pipeline logic arrives byte-identical; the
         P4-specific edits are applied afterwards as small patches.

      2. Vendors the Waveshare board-support components out of the
         ESP32-P4-WIFI6-Touch-LCD-5 repository into ./components. These are
         not published on the ESP component registry, so they have to come
         from git.

    Run from this directory:  .\setup.ps1
#>

$ErrorActionPreference = 'Stop'

$here    = $PSScriptRoot
$s3main  = Join-Path (Split-Path $here -Parent) 'main'
$p4main  = Join-Path $here 'main'
$compDir = Join-Path $here 'components'

# ---------------------------------------------------------------- 1. app sources
Write-Host '== copying application sources from the S3 project ==' -ForegroundColor Cyan

if (-not (Test-Path $s3main)) {
    throw "Cannot find the S3 project's main directory at $s3main"
}

$appFiles = @(
    'main.c',
    'audio_pipeline.c', 'audio_pipeline.h',
    'audiosense_mqtt.c', 'audiosense_mqtt.h'
)

foreach ($f in $appFiles) {
    $src = Join-Path $s3main $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $p4main $f) -Force
        Write-Host "  copied $f"
    } else {
        Write-Warning "  missing $f (skipped)"
    }
}

$certSrc = Join-Path $s3main 'certs'
$certDst = Join-Path $p4main 'certs'
if (Test-Path $certSrc) {
    # Clear the destination first: Copy-Item -Recurse onto an existing
    # directory nests it (certs\certs) instead of replacing it.
    if (Test-Path $certDst) { Remove-Item $certDst -Recurse -Force }
    Copy-Item $certSrc $certDst -Recurse -Force
    Write-Host '  copied certs/'
} else {
    Write-Warning '  missing certs/ (skipped) - EMBED_TXTFILES will fail without it'
}

# ---------------------------------------------------------- 2. Waveshare BSP
Write-Host '== vendoring Waveshare board support ==' -ForegroundColor Cyan

$repo = 'https://github.com/waveshareteam/ESP32-P4-WIFI6-Touch-LCD-5.git'
$tmp  = Join-Path ([System.IO.Path]::GetTempPath()) 'ws-p4-bsp'

# The BSP lives inside an example rather than at the repo root.
$srcRel = 'examples/esp-idf/08_lvgl_demo_v9/components'

if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }

# Blobless sparse clone: the repo carries firmware images and schematics we
# do not want to download.
git clone --depth 1 --filter=blob:none --sparse $repo $tmp
git -C $tmp sparse-checkout set $srcRel

$src = Join-Path $tmp ($srcRel -replace '/', '\')
New-Item -ItemType Directory -Force -Path $compDir | Out-Null

# bsp_extra is deliberately NOT vendored: it wraps playback in the
# audio_player / file_iterator abstraction, which this application does not
# use (it drives esp_codec_dev directly). Skipping it avoids pulling those
# two extra dependencies in.
foreach ($c in @('esp32_p4_wifi6_touch_lcd_5', 'esp_lcd_hx8394')) {
    $from = Join-Path $src $c
    if (-not (Test-Path $from)) { throw "Expected component not found in repo: $c" }
    $to = Join-Path $compDir $c
    if (Test-Path $to) { Remove-Item $to -Recurse -Force }
    Copy-Item $from $to -Recurse -Force
    Write-Host "  vendored $c"
}

Remove-Item $tmp -Recurse -Force

Write-Host ''
Write-Host 'Setup complete. Next:' -ForegroundColor Green
Write-Host '  idf.py set-target esp32p4'
Write-Host '  idf.py build'
