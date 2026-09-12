<#
.SYNOPSIS
    Check, commit and push ComfyUI System Monitor to GitHub.

.DESCRIPTION
    Runs a safety checklist before anything leaves the machine:
      1. git is installed
      2. no secret or runtime file is staged (sysmon_config.json, logs/)
      3. no API-key-shaped string exists anywhere in the sources
      4. backend tests and web static checks pass
    Only then does it initialise the repo, commit and push.

    Without -RepoUrl it stops after the checks and prints what it would do,
    so it is safe to run just to validate the tree.

.EXAMPLE
    # dry run: only validate
    .\scripts\publish.ps1

.EXAMPLE
    # validate, commit and push
    .\scripts\publish.ps1 -RepoUrl https://github.com/me/comfyui-sysmon.git

.EXAMPLE
    # create the GitHub repo too (needs GitHub CLI)
    .\scripts\publish.ps1 -UseGhCli
#>
[CmdletBinding()]
param(
    [string]$RepoUrl,
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$CommitMessage,
    [switch]$UseGhCli,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$script:Failures = 0
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:Failures++ }
function Info($m) { Write-Host "  $m" -ForegroundColor Gray }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

Write-Host "ComfyUI System Monitor - publish helper" -ForegroundColor White
Write-Host "repo root: $Root"

# --------------------------------------------------------------------------
Head "1. git available"
$git = Get-Command git -ErrorAction SilentlyContinue
$gitOk = $false
if ($git) {
    Ok "git found: $($git.Source)"
    Info (& git --version)
    $gitOk = $true
} else {
    # Not fatal on its own: the checks below are still worth running, and this
    # is the normal state before Git is installed for the first time.
    Bad "git not found. Install it first: winget install --id Git.Git -e"
    Info "(continuing with the remaining checks; publishing needs git)"
}

# --------------------------------------------------------------------------
Head "2. secrets and runtime files must not be tracked"
$forbidden = @('sysmon_config.json')
foreach ($name in $forbidden) {
    if (Test-Path (Join-Path $Root $name)) {
        Info "present locally (fine, must stay untracked): $name"
    }
}
# .gitignore must cover both.
$ignore = Get-Content (Join-Path $Root '.gitignore') -Raw
if ($ignore -match 'sysmon_config\.json') { Ok ".gitignore excludes sysmon_config.json" }
else { Bad ".gitignore does not exclude sysmon_config.json" }
if ($ignore -match '(?m)^logs/') { Ok ".gitignore excludes logs/" }
else { Bad ".gitignore does not exclude logs/" }

# --------------------------------------------------------------------------
Head "3. no API-key-shaped strings in sources"
# Real keys are long and random. The test suite necessarily contains fake ones,
# so instead of guessing from keywords in the line (which false-positived on the
# word "test" appearing anywhere in it), only the exact fixtures below are
# excused. Anything else that looks like a key is reported - including a real key
# that someone pasted into a test file.
$keyPattern = '(sk|sk-proj|sk-or|gsk|xai)-[A-Za-z0-9_\-]{16,}'
$knownFixtures = @(
    'sk-mock-abcdefghijklmnopqrstuvwxyz',   # tests/api_tests.py
    'sk-abcdefghijklmnop',                  # tests/run_tests.py
    'sk-test-key-abcdefghijklmnop',         # tests/run_tests.py failure modes
    'sk-SUPERSECRET-abcdef1234567890',      # tests/run_tests.py leak regression
    'sk-invalid-key-for-testing-123456',    # manual probe in docs
    'sk-ui-roundtrip-abcdefghijklmnopqrstuvwxyz',
    'sk-deliberately-invalid-key-1234567890'
)
$files = Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -notmatch '\\(__pycache__|\.git|logs)\\' -and
        $_.Extension -in @('.py', '.js', '.mjs', '.json', '.md', '.toml', '.yml', '.yaml', '.css', '.ps1')
    }
$hits = @()
$ignored = 0
foreach ($file in $files) {
    foreach ($m in (Select-String -Path $file.FullName -Pattern $keyPattern -AllMatches -ErrorAction SilentlyContinue)) {
        foreach ($match in $m.Matches) {
            if ($knownFixtures -contains $match.Value) { $ignored++; continue }
            $hits += [pscustomobject]@{ Path = $file.FullName; Line = $m.LineNumber; Value = $match.Value }
        }
    }
}
if ($hits) {
    Bad "possible hardcoded key(s):"
    $hits | ForEach-Object { Info ("  {0}:{1}  {2}" -f $_.Path.Replace($Root, '.'), $_.Line, $_.Value.Substring(0, [Math]::Min(16, $_.Value.Length)) + '...') }
} else {
    Ok "no real-looking API keys found"
    if ($ignored) { Info "(excused $ignored known test fixture(s))" }
}

# --------------------------------------------------------------------------
if (-not $SkipTests) {
    Head "4. tests"
    $py = $null
    foreach ($candidate in @(
        (Join-Path (Split-Path -Parent (Split-Path -Parent $Root)) '.venv\Scripts\python.exe'),
        'python', 'python3'
    )) {
        if ($candidate -and (Test-Path $candidate)) { $py = $candidate; break }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }
    if ($py) {
        Info "python: $py"
        & $py (Join-Path $Root 'tests\run_tests.py') | Select-Object -Last 3
        if ($LASTEXITCODE -eq 0) { Ok "backend tests passed" } else { Bad "backend tests failed" }
    } else {
        Bad "no python interpreter found for tests (use -SkipTests to bypass)"
    }

    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node (Join-Path $Root 'tests\check_web.mjs') | Select-Object -Last 2
        if ($LASTEXITCODE -eq 0) { Ok "web checks passed" } else { Bad "web checks failed" }
    } else {
        Info "node not found; skipping web checks"
    }
} else {
    Head "4. tests (skipped)"
}

# --------------------------------------------------------------------------
if ($script:Failures -gt 0) {
    Write-Host "`n$($script:Failures) check(s) failed - not publishing." -ForegroundColor Red
    exit 1
}
Write-Host "`nAll pre-flight checks passed." -ForegroundColor Green

if (-not $gitOk) {
    Write-Host "`ngit is still required to publish. After installing it, re-run this script." -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------------------
if ($UseGhCli) {
    Head "5. create repository with GitHub CLI"
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Bad "gh not found. Install: winget install --id GitHub.cli -e"
        exit 1
    }
    $repoName = Split-Path -Leaf $Root
    Info "creating '$repoName' (public)"
    & gh repo create $repoName --public --source=. --remote=$Remote --push
    if ($LASTEXITCODE -eq 0) { Ok "pushed via gh"; exit 0 } else { Bad "gh repo create failed"; exit 1 }
}

if (-not $RepoUrl) {
    Head "5. dry run complete"
    Write-Host @"
No -RepoUrl given, so nothing was committed or pushed.

To publish, re-run with your repository URL:

  .\scripts\publish.ps1 -RepoUrl https://github.com/<you>/comfyui-sysmon.git

or let the GitHub CLI create it:

  .\scripts\publish.ps1 -UseGhCli
"@ -ForegroundColor Yellow
    exit 0
}

# --------------------------------------------------------------------------
Head "5. initialise, commit, push"
if (-not (Test-Path (Join-Path $Root '.git'))) {
    & git init -b $Branch | Out-Null
    Ok "initialised repository on branch '$Branch'"
} else {
    Ok "repository already initialised"
    & git rev-parse --abbrev-ref HEAD | Out-Null
}

& git add -A
# Never stage the secret or the runtime logs, whatever happens.
& git reset -q -- sysmon_config.json 2>$null
& git reset -q -- logs 2>$null

$staged = & git diff --cached --name-only
$leak = $staged | Where-Object { $_ -match 'sysmon_config\.json|^logs/' }
if ($leak) {
    Bad "refusing to commit secret/runtime files: $($leak -join ', ')"
    exit 1
}
Ok "$($staged.Count) file(s) staged"

if (-not $CommitMessage) {
    $CommitMessage = @"
feat: ComfyUI system monitor with per-node peak attribution

- live GPU/VRAM/CPU/RAM/disk sampling (pynvml -> nvidia-smi -> torch fallback)
- per-node peak attribution via ComfyUI's ProgressRegistry
- run duration + traceback logging with JSON persistence
- optional AI diagnosis via DeepSeek / any OpenAI-compatible endpoint
- zero runtime dependencies; backend tests + web static checks
"@
}

& git commit -q -m $CommitMessage
if ($LASTEXITCODE -ne 0) { Bad "git commit failed"; exit 1 }
Ok "committed"

$existing = & git remote
if ($existing -notcontains $Remote) {
    & git remote add $Remote $RepoUrl
    Ok "remote '$Remote' -> $RepoUrl"
} else {
    Ok "remote '$Remote' already configured"
}

& git push -u $Remote $Branch
if ($LASTEXITCODE -eq 0) { Ok "pushed to $Remote/$Branch" } else { Bad "push failed"; exit 1 }

Write-Host "`nPublished." -ForegroundColor Green
