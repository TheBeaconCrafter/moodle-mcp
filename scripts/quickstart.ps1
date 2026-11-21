# moodle-mcp quickstart for Windows (PowerShell 5.1 or 7).
#
#   irm https://raw.githubusercontent.com/TheBeaconCrafter/moodle-mcp/main/scripts/quickstart.ps1 | iex
#
# Installs uv (if missing) and moodle-mcp, asking first, then starts `moodle quickstart`: a guided setup
# that signs you in to Moodle and connects it to ChatGPT through OpenAI's Secure MCP Tunnel.
# Everything runs inside a script block, so stopping early never closes your PowerShell window.

& {
    $ErrorActionPreference = 'Stop'
    $repo = 'git+https://github.com/TheBeaconCrafter/moodle-mcp'

    function Ask([string]$question) {
        $answer = Read-Host "$question [Y/n]"
        return ($answer -eq '' -or $answer -match '^(y|yes)$')
    }

    # uv installs into ~\.local\bin, which this window may not have on PATH yet.
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if ($env:Path -notlike "*$localBin*") { $env:Path = "$localBin;$env:Path" }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        if (-not (Ask "moodle-mcp is installed with uv (Astral's Python tool installer), which isn't here yet. Install uv from astral.sh?")) {
            Write-Host 'Stopped. See https://docs.astral.sh/uv/ to install uv yourself.'
            return
        }
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Write-Host 'uv was installed but isn''t on PATH yet. Open a new PowerShell window and run this again.'
            return
        }
    }

    $installed = [bool](Get-Command moodle -ErrorAction SilentlyContinue)
    $question = if ($installed) { 'Update moodle-mcp to the latest version?' } else { 'Install moodle-mcp (the moodle and moodle-mcp commands)?' }
    if (Ask $question) {
        uv tool install --force $repo
        if ($LASTEXITCODE -ne 0) { Write-Host 'Installing moodle-mcp failed.'; return }
        uv tool update-shell *> $null
    } elseif (-not $installed) {
        Write-Host "Stopped: moodle-mcp isn't installed."
        return
    }

    $bin = (uv tool dir --bin).Trim()
    $moodle = Join-Path $bin 'moodle.exe'
    if (-not (Test-Path $moodle)) { $moodle = 'moodle' }
    & $moodle quickstart
}
