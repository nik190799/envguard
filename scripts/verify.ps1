# Cadence verify orchestrator (PowerShell).
#
# Reads .cadence/cadence.yaml and runs:
#   1. commands.format    (each entry as a separate command)
#   2. commands.lint
#   3. tool/check_boundaries.py
#   4. commands.test
#
# Fails fast on first non-zero exit. Prints a Definition-of-Done-shaped
# summary at the end.
#
# Writes verify evidence for tool/compliance_report.py under .cadence/:
#   last_verify.log   this run's output
#   .last_verify_ok   present only after a fully green run
#   .last_verify_sha  the commit verified ("-dirty" if the tree had changes)
# Stale .last_verify_ok / .last_verify_sha are removed before every run, so
# a failed or interrupted run never leaves an old pass behind.
#
# Usage:  pwsh -File scripts/verify.ps1
# Exit:   0 = all green; non-zero = first failing step's exit code.

$ErrorActionPreference = 'Stop'
$start = Get-Date

$Config = if ($env:CADENCE_CONFIG) { $env:CADENCE_CONFIG } else { '.cadence/cadence.yaml' }
$Root   = if ($env:CADENCE_ROOT)   { $env:CADENCE_ROOT }   else { (Get-Location).Path }
$ConfigPath = Join-Path $Root $Config

$EvidenceDir = Join-Path $Root '.cadence'
$OkMarker    = Join-Path $EvidenceDir '.last_verify_ok'
$ShaMarker   = Join-Path $EvidenceDir '.last_verify_sha'
$LogFile     = Join-Path $EvidenceDir 'last_verify.log'
Remove-Item -LiteralPath $OkMarker, $ShaMarker -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $ConfigPath)) {
    Write-Host "FAIL: cadence config not found at $Config" -ForegroundColor Red
    Write-Host 'Hint: run /cadence-init to scaffold one.'
    exit 2
}

$Python = (Get-Command python -ErrorAction SilentlyContinue) ?? (Get-Command python3 -ErrorAction SilentlyContinue)
if (-not $Python) {
    Write-Host 'FAIL: python is required (install Python 3.10+)' -ForegroundColor Red
    exit 2
}

# Record which commit is being verified before anything runs, so files the
# run itself writes cannot mark the tree dirty.
$VerifiedSha = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $head = git -C $Root rev-parse --verify -q HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $head) {
        $VerifiedSha = "$head".Trim()
        $dirty = git -C $Root status --porcelain -- . `
            ':(exclude).cadence/.last_verify_ok' `
            ':(exclude).cadence/.last_verify_sha' `
            ':(exclude).cadence/last_verify.log' 2>$null
        if ($dirty) { $VerifiedSha += '-dirty' }
    }
}

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
Set-Content -LiteralPath $LogFile -Value '' -NoNewline

# Print a line and append it to last_verify.log.
function Say {
    param([string] $Text = '', [string] $Color = '')
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    Add-Content -LiteralPath $LogFile -Value $Text
}

function Read-Commands {
    param([string] $Section)

    $script = @"
import sys
try:
    import yaml
except ImportError:
    print('__MISSING_PYYAML__')
    sys.exit(0)
path, section = sys.argv[1], sys.argv[2]
with open(path, 'r', encoding='utf-8') as fh:
    cfg = yaml.safe_load(fh) or {}
cmds = (cfg.get('commands') or {}).get(section) or []
if isinstance(cmds, str):
    cmds = [cmds]
for c in cmds:
    print(c)
"@

    $output = & $Python.Source -c $script $ConfigPath $Section 2>&1
    if ($LASTEXITCODE -ne 0) {
        Say "FAIL: reading commands.$Section from $Config" Red
        $output | ForEach-Object { Say "$_" }
        exit 2
    }
    return $output | Where-Object { $_ -ne '' }
}

function Invoke-Step {
    param(
        [string] $Label,
        [string[]] $Commands
    )

    Say ''
    Say "==> $Label" Cyan

    if (-not $Commands -or $Commands.Count -eq 0) {
        Say "(no commands configured for $Label; skipping)" DarkGray
        return
    }

    foreach ($cmd in $Commands) {
        if ($cmd -eq '__MISSING_PYYAML__') {
            Say 'FAIL: PyYAML not installed (pip install pyyaml)' Red
            exit 2
        }
        Say "`$ $cmd" DarkGray
        cmd /c $cmd 2>&1 | ForEach-Object { Say "$_" }
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Say "FAIL: $Label (exit $rc)" Red
            exit $rc
        }
    }
}

Set-Location $Root

Invoke-Step 'format' (Read-Commands 'format')
Invoke-Step 'lint'   (Read-Commands 'lint')

# Boundary check is built-in.
if (Test-Path (Join-Path $Root 'tool/check_boundaries.py')) {
    Say ''
    Say '==> boundaries' Cyan
    Say '$ python tool/check_boundaries.py' DarkGray
    & $Python.Source 'tool/check_boundaries.py' '--config' $Config 2>&1 | ForEach-Object { Say "$_" }
    $rc = $LASTEXITCODE
    if ($rc -ne 0) {
        Say "FAIL: boundaries (exit $rc)" Red
        exit $rc
    }
} else {
    Say ''
    Say '(tool/check_boundaries.py not found; skipping boundary check)' DarkGray
}

Invoke-Step 'test' (Read-Commands 'test')

$elapsed = (Get-Date) - $start
Say ''
Say ("OK ({0:N1}s)" -f $elapsed.TotalSeconds) Green
Say ''
Say 'Definition of Done (mechanical, auto-enforced):'
Say '  format'
Say '  lint'
Say '  boundaries'
Say '  test'
Say ''
Say 'Manual DoD lines (Reviewer responsibility) - see docs/DEFINITION_OF_DONE.md.'

Set-Content -LiteralPath $OkMarker -Value ('ok ' + (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"))
if ($VerifiedSha) { Set-Content -LiteralPath $ShaMarker -Value $VerifiedSha }
exit 0
