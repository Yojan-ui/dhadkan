<#
.SYNOPSIS
  Dhadkan tasks on native Windows (PowerShell 5.1+). Mirrors the Makefile, which stays the Linux/macOS path.

.EXAMPLE
  .\dev.ps1 setup        # venv (Python 3.12), Python and web dependencies
  .\dev.ps1 test         # pytest -q, then the web tests
  .\dev.ps1 lint         # ruff, black --check, eslint, prettier --check, tsc -b (zero warnings)
  .\dev.ps1 demo-ims     # Digital Twin + NASA IMS bearing failure + agent (what npm run demo runs)

  Tasks: setup data demo-data train results demo demo-ims serve dev test lint fmt clean compare rul
         mill mill-pack eval-agent llm-check kb-build
         kb-search demo-agents tunnel live-trial voice-models
         tts-eval help.  $env:PORT overrides the port (default 8600).
  Exits non-zero when any step fails.
#>
param(
    [Parameter(Position = 0)][string]$Task = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Web = Join-Path $Root "web"
$Port = if ($env:PORT) { $env:PORT } else { "8600" }
$PyDirs = @("edge", "server", "data", "tests", "tools", "scripts")
Set-Location $Root

function Invoke-Native {
    <# Run a native command; stop the task with its exit code if it fails. #>
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED ($LASTEXITCODE): $Exe $($Arguments -join ' ')" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

function Invoke-InWeb {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    Push-Location $Web
    try { Invoke-Native $Exe $Arguments } finally { Pop-Location }
}

function Assert-Venv {
    if (-not (Test-Path $Py)) {
        Write-Host "No .venv yet: run .\dev.ps1 setup" -ForegroundColor Red
        exit 1
    }
}

function Get-Newest {
    <# Newest write time among files under the given paths (files or folders). #>
    param([string[]]$Paths)
    $newest = [datetime]::MinValue
    foreach ($p in $Paths) {
        if (-not (Test-Path $p)) { continue }
        $items = if ((Get-Item $p).PSIsContainer) { Get-ChildItem $p -Recurse -File } else { Get-Item $p }
        foreach ($i in $items) { if ($i.LastWriteTimeUtc -gt $newest) { $newest = $i.LastWriteTimeUtc } }
    }
    return $newest
}

function Update-WebModules {
    <# npm ci when node_modules is missing or older than the lockfile (Makefile: web/node_modules). #>
    $mods = Join-Path $Web "node_modules"
    $lock = Join-Path $Web "package-lock.json"
    if (-not (Test-Path $mods) -or (Get-Item $lock).LastWriteTimeUtc -gt (Get-Item $mods).LastWriteTimeUtc) {
        Invoke-InWeb "npm" @("ci", "--no-audit", "--no-fund")
        (Get-Item $mods).LastWriteTimeUtc = [datetime]::UtcNow
    }
}

function Update-WebDist {
    <# Rebuild web/dist when any web source is newer than dist/index.html (Makefile: web/dist/index.html). #>
    Update-WebModules
    $index = Join-Path $Web "dist\index.html"
    $src = @("src", "index.html", "public", "package.json", "vite.config.ts", "node_modules") | ForEach-Object { Join-Path $Web $_ }
    if (-not (Test-Path $index) -or (Get-Newest $src) -gt (Get-Item $index).LastWriteTimeUtc) {
        Invoke-InWeb "npm" @("run", "build")
    }
}

switch ($Task) {
    "setup" {
        if (-not (Test-Path $Py)) { Invoke-Native "py" @("-3.12", "-m", "venv", ".venv") }
        Invoke-Native $Py @("-m", "pip", "install", "-q", "--upgrade", "pip")
        Invoke-Native $Py @("-m", "pip", "install", "-q", "-r", "requirements.txt", "-r", "requirements-dev.txt")
        Invoke-InWeb "npm" @("ci", "--no-audit", "--no-fund")
    }
    "data" { Assert-Venv; Invoke-Native $Py @("-m", "data.real"); Invoke-Native $Py @("-m", "data.download") }
    "demo-data" { Assert-Venv; Invoke-Native $Py @("-m", "data.real", "demo"); Invoke-Native $Py @("-m", "data.download") }
    "train" {
        Assert-Venv
        foreach ($m in @("edge.train", "edge.faulttype", "edge.ims", "edge.results")) { Invoke-Native $Py @("-m", $m) }
    }
    "results" { Assert-Venv; Invoke-Native $Py @("-m", "edge.results") }
    "demo" { Assert-Venv; Update-WebDist; Invoke-Native $Py (@("-m", "server.demo", "--port", $Port) + $Rest) }
    "demo-ims" { Assert-Venv; Update-WebDist; Invoke-Native $Py (@("-m", "server.demo", "--port", $Port, "--ims") + $Rest) }
    "serve" { Assert-Venv; Update-WebDist; Invoke-Native $Py @("-m", "server.demo", "--port", $Port, "--no-replay") }
    "dev" {
        Assert-Venv
        Update-WebModules
        $backend = Start-Process -FilePath $Py -ArgumentList @("-m", "server.demo", "--port", $Port, "--no-replay", "--no-open") -PassThru -NoNewWindow
        try { Invoke-InWeb "npx" @("vite", "--port", "5173", "--open") }
        finally { if (-not $backend.HasExited) { Stop-Process -Id $backend.Id -Force } }
    }
    "compare" { Assert-Venv; Invoke-Native $Py @("-m", "edge.compare") }
    "rul" { Assert-Venv; Invoke-Native $Py @("-m", "edge.rul") }
    "llm-check" { Assert-Venv; Invoke-Native $Py @("-m", "server.llm_check") }
    "kb-build" { Assert-Venv; Invoke-Native $Py @("-m", "server.kb", "build") }
    "demo-agents" { Assert-Venv; Invoke-Native $Py (@("-m", "server.a2a.demo_agents") + $Rest) }
    "tunnel" { Assert-Venv; Invoke-Native $Py (@("-m", "scripts.tunnel", "--port", $Port) + $Rest) }
    "live-trial" { Assert-Venv; Invoke-Native $Py (@("-m", "scripts.live_trial") + $Rest) }
    "voice-models" {
        # optional: voice notes. CPU torch (PyTorch's CPU index), sherpa-onnx, transformers, then the models
        Assert-Venv
        Invoke-Native $Py @("-m", "pip", "install", "-q", "torch", "--index-url", "https://download.pytorch.org/whl/cpu")
        Invoke-Native $Py @("-m", "pip", "install", "-q", "-r", (Join-Path $Root "requirements-voice.txt"))
        Invoke-Native $Py @("-m", "scripts.fetch_voice_models")
    }
    "tts-eval" { Assert-Venv; Invoke-Native $Py (@("-m", "scripts.tts_eval") + $Rest) }
    "kb-search" { Assert-Venv; Invoke-Native $Py (@("-m", "server.kb", "search") + $Rest) }
    "mill" { Assert-Venv; Invoke-Native $Py @("-m", "server.flourmill", "--auto") }
    "mill-pack" { Assert-Venv; Invoke-Native $Py @("-m", "server.flourmill", "--pack") }
    "eval-agent" {
        # the planner's scenarios: the mock policy (no model); `.\dev.ps1 eval-agent --live` runs them on local Granite
        Assert-Venv
        if ($Rest -contains "--live") { Write-Host "eval-agent: LIVE on local Granite (Ollama); slow on CPU, costs nothing" }
        else { Write-Host "eval-agent: mock policy (no model). Add --live for Granite on Ollama." }
        Invoke-Native $Py (@("-m", "server.planner.evaluate") + $Rest)
        Invoke-Native $Py @("-m", "edge.results")
    }
    "test" {
        Assert-Venv
        Invoke-Native $Py (@("-m", "pytest", "-q") + $Rest)
        Invoke-InWeb "npm" @("test", "--silent")
    }
    "lint" {
        Assert-Venv
        Invoke-Native $Py @("-m", "ruff", "check", ".")
        Invoke-Native $Py (@("-m", "black", "--check", "-q") + $PyDirs)
        Invoke-InWeb "npx" @("eslint", ".", "--max-warnings", "0")
        Invoke-InWeb "npx" @("prettier", "--check", ".", "--log-level", "warn")
        Invoke-InWeb "npx" @("tsc", "-b")
    }
    "fmt" {
        Assert-Venv
        Invoke-Native $Py @("-m", "ruff", "check", ".", "--fix", "-q")
        Invoke-Native $Py (@("-m", "black", "-q") + $PyDirs)
        Invoke-InWeb "npx" @("prettier", "--write", ".", "--log-level", "warn")
    }
    "clean" {
        foreach ($p in @("web\dist", ".pytest_cache", ".ruff_cache")) {
            if (Test-Path $p) { Remove-Item -Recurse -Force $p -Confirm:$false }
        }
    }
    { $_ -in @("help", "-h", "--help", "/?") } { Get-Help $PSCommandPath -Detailed | Out-Host }
    default {
        Write-Host "Unknown task '$Task'. Run .\dev.ps1 help" -ForegroundColor Red
        exit 2
    }
}
exit 0
