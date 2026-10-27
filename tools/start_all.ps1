# Start every long-running paper bot / recorder that isn't already running, from PowerShell.
# Paper only; nothing here signs a transaction. Safe to re-run: programs already running are skipped.
# Each program runs in its own hidden window, so closing this terminal (or Claude Code) does NOT stop it.
# The program list is read from tools/start_all.sh (one list for both scripts).
#
#   powershell -ExecutionPolicy Bypass -File tools\start_all.ps1          # start what's missing
#   powershell -ExecutionPolicy Bypass -File tools\start_all.ps1 -List    # only show running / missing
#   powershell -ExecutionPolicy Bypass -File tools\start_all.ps1 -Stop    # stop every program in the list
param([switch]$List, [switch]$Stop)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { Write-Host "uv not found on PATH. Install it or open the terminal you normally run 'uv run' in." -ForegroundColor Red; exit 1 }

# rows look like:   'pattern|command after "uv run python"|logname'
$programs = @()
foreach ($line in Get-Content (Join-Path $PSScriptRoot "start_all.sh")) {
    if ($line -match "^\s*'([^']+)'\s*$") {
        $parts = $Matches[1].Split('|')
        if ($parts.Count -eq 3) { $programs += [pscustomobject]@{ Pattern = $parts[0]; Cmd = $parts[1]; Log = $parts[2] } }
    }
}
if ($programs.Count -eq 0) { Write-Host "No programs found in tools/start_all.sh" -ForegroundColor Red; exit 1 }

function Get-PythonProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine }
}

$procs = Get-PythonProcs
$started = 0
foreach ($p in $programs) {
    $match = $procs | Where-Object { $_.CommandLine -match $p.Pattern }
    if ($Stop) {
        foreach ($m in $match) { Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue }
        if ($match) { Write-Host ("stopped  {0}" -f $p.Log) }
        continue
    }
    if ($match) { Write-Host ("running  {0}" -f $p.Log) -ForegroundColor Green; continue }
    if ($List) { Write-Host ("MISSING  {0}" -f $p.Log) -ForegroundColor Yellow; continue }
    $log = "logs\{0}.out" -f $p.Log
    Write-Host ("starting {0}  ({1})" -f $p.Log, $log) -ForegroundColor Cyan
    $inner = "set PYTHONIOENCODING=utf-8&& `"$uv`" run python $($p.Cmd) >> `"$log`" 2>&1"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $inner -WorkingDirectory $root -WindowStyle Hidden
    $started++
    Start-Sleep -Seconds 2
}
if (-not $List -and -not $Stop) {
    Write-Host ""
    Write-Host ("{0} started. Check in a minute with:  powershell -ExecutionPolicy Bypass -File tools\start_all.ps1 -List" -f $started)
    Write-Host "Dashboard: http://127.0.0.1:8765   Logs: logs\<name>.out"
    Write-Host "Keep the PC awake (Settings > System > Power > Sleep: Never) or the bots pause while it sleeps."
}
