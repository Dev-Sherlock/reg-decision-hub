<#
.SYNOPSIS
  Download a GGUF model into ./models so the `llm` container can serve it.

.DESCRIPTION
  PowerShell counterpart to scripts/download_model.sh, for Windows shells that
  have no bash. Same presets, same resumable behaviour: the download lands in
  <name>.part and a re-run continues from where it stopped.

  Presets:
    tiny            SmolLM2-135M-Instruct Q4_K_M, 101MiB - the default. Smallest
                    model that still answers coherently; fastest on CPU.
    tinyllama-1.1b  TinyLlama-1.1B-Chat v1.0 Q4_K_M, 638MiB - noticeably better
                    answers, roughly 3x slower on CPU.

  The filename written here MUST match MODEL_PATH in your .env, because the
  `llm` container mounts ./models read-only and reads that exact path.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1 tinyllama-1.1b

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1 <repo> <filename>
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Preset = 'tiny',

    [Parameter(Position = 1)]
    [string] $Filename,

    [Parameter(Position = 2)]
    [string] $Repo
)

$ErrorActionPreference = 'Stop'

$Presets = @{
    'tiny'           = @{ Repo = 'bartowski/SmolLM2-135M-Instruct-GGUF'; Filename = 'SmolLM2-135M-Instruct-Q4_K_M.gguf' }
    'tinyllama-1.1b' = @{ Repo = 'TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF'; Filename = 'tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf' }
}
# Alias, assigned after the literal: a hashtable cannot reference itself while it
# is still being built.
$Presets['tinyllama'] = $Presets['tinyllama-1.1b']

if ($Repo) {
    # Explicit repo wins over a preset name.
    $Resolved = @{ Repo = $Repo; Filename = $Filename }
}
elseif ($Presets.ContainsKey($Preset)) {
    $Resolved = $Presets[$Preset]
}
else {
    # Bare filename in position 0 is not a preset; treat it as <repo> <filename>.
    if ($Filename) {
        $Resolved = @{ Repo = $Preset; Filename = $Filename }
    }
    else {
        Write-Error "unknown preset '$Preset'. Use: tiny | tinyllama-1.1b | <repo> <filename>"
        exit 2
    }
}

$Repo = $Resolved.Repo
$Filename = $Resolved.Filename

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ModelsDir = Join-Path $ProjectRoot 'models'
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

$Target = Join-Path $ModelsDir $Filename

if (Test-Path -LiteralPath $Target) {
    $size = (Get-Item -LiteralPath $Target).Length
    Write-Host "Model already present: $Target ($size bytes)"
    Write-Host "Set MODEL_PATH=/models/$Filename in your .env"
    Write-Host 'Other presets: tiny | tinyllama-1.1b'
    exit 0
}

$Url = "https://huggingface.co/$Repo/resolve/main/$Filename"
$Part = "$Target.part"

# curl.exe, not the PowerShell `curl` alias, which is Invoke-WebRequest and
# knows neither -C nor --retry-delay.
$curlArgs = @('-fL', '--progress-bar', '--retry', '3', '--retry-delay', '5', '-C', '-', '-o', $Part)

if ($env:HF_TOKEN) {
    Write-Host 'Using HF_TOKEN for authenticated download.'
    $curlArgs += @('-H', "Authorization: Bearer $env:HF_TOKEN")
}
else {
    Write-Host 'No HF_TOKEN set - this only works for public repositories.'
}

Write-Host "Downloading $Repo/$Filename"
Write-Host "  -> $Part"

& curl.exe @curlArgs $Url
$curlExit = $LASTEXITCODE

# Guard against the LFS pointer file that is served instead of the real blob.
$isLfsPointer = $false
if (Test-Path -LiteralPath $Part) {
    $stream = [System.IO.File]::OpenRead($Part)
    try {
        $buffer = New-Object byte[] 20
        $read = $stream.Read($buffer, 0, 20)
        $head = [System.Text.Encoding]::ASCII.GetString($buffer, 0, $read)
        $isLfsPointer = $head -like '*version https://git-lfs*'
    }
    finally {
        $stream.Dispose()
    }
}

if ($isLfsPointer -or (Test-Path -LiteralPath $Part) -and (Get-Item -LiteralPath $Part).Length -eq 0) {
    Remove-Item -LiteralPath $Part -Force
    Write-Error 'download did not produce a GGUF file. The repository is probably gated - set HF_TOKEN and retry.'
    exit 1
}

if ($curlExit -ne 0) {
    # The .part is deliberately kept so the next run resumes instead of
    # re-fetching hundreds of megabytes.
    Write-Host "download interrupted (curl exit $curlExit)" -ForegroundColor Yellow
    Write-Host 'Re-run the same command to resume from where it stopped.' -ForegroundColor Yellow
    exit 1
}

Move-Item -LiteralPath $Part -Destination $Target -Force
$size = (Get-Item -LiteralPath $Target).Length
Write-Host "Done: $Target ($size bytes)"
Write-Host "Set MODEL_PATH=/models/$Filename in your .env"
Write-Host 'Then: docker compose up -d llm'