<#
.SYNOPSIS
    Publish this repository to GitHub, with safety checks.

.DESCRIPTION
    Run this in YOUR OWN PowerShell window, not through an automation sandbox.
    Pushing needs the Git credential manager, which communicates over a Windows
    named pipe; sandboxed environments block that with
    "sh.exe: couldn't create signal pipe, Win32 error 5".

    The script refuses to run if a secret would be published -- that check is the
    whole point, because `git add -A` on a machine that has a .env file is the
    single most common way API keys end up on GitHub.

.EXAMPLE
    # after creating an EMPTY repo at https://github.com/cassidycai/roostoo-quant-bot
    .\scripts\publish.ps1

.EXAMPLE
    .\scripts\publish.ps1 -User myname -Repo my-bot-name -VisibilityHint private
#>
[CmdletBinding()]
param(
    [string]$User = 'cassidycai',
    [string]$Repo = 'roostoo-quant-bot',
    [string]$Branch = 'main',
    [string]$RemoteUrl = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Fail($message) {
    Write-Host ""
    Write-Host "FAILED: $message" -ForegroundColor Red
    exit 1
}

Write-Host "Publishing $repoRoot" -ForegroundColor Cyan

# --- 1. Never publish a secret -------------------------------------------------
Write-Host "[1/5] scanning for secrets (filenames, contents and history)..."

# 1a. Filenames that must never be tracked at all.
$tracked = git ls-files
if (-not $tracked) { Fail "nothing is tracked by git yet." }

$secretPattern = '(^|/)\.env$|\.env\.(local|production|secret)$|\.pem$|\.key$|id_rsa|credentials\.json$'
$offenders = $tracked | Select-String -Pattern $secretPattern
if ($offenders) {
    Write-Host "These tracked files look like secrets:" -ForegroundColor Yellow
    $offenders | ForEach-Object { Write-Host "  $_" }
    Fail @"
refusing to publish. Remove them from the index (the file stays on disk):
    git rm --cached <file>
    git commit -m "chore: stop tracking <file>"
then rotate the key, because it is already in your local history.
"@
}

# 1b. CONTENTS, in the index, the worktree and every commit reachable from any
#     ref. Step 1a alone is what this script used to do, and that is exactly how
#     a hardcoded API key and secret inside a .py file were published to a public
#     repository: the filename looked innocuous, so nothing ever read the bytes.
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $python = $candidate; break }
}
if (-not $python) {
    Fail "no python on PATH. The content/history secret scan needs it; run 'python scripts/scan_secrets.py --all' yourself before publishing."
}
& $python 'scripts/scan_secrets.py' '--all'
if ($LASTEXITCODE -ne 0) {
    Fail @"
refusing to publish: the secret scan found credentials (output above).
Rotate the key with the issuer FIRST -- deleting the commit does not unpublish
it -- then remove it from history. See docs/SECURITY.md.
"@
}
Write-Host "      no secrets in filenames, contents or history" -ForegroundColor Green

# --- 2. Sanity-check the local repository -------------------------------------
Write-Host "[2/5] checking local repository..."
$branchNow = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branchNow -ne $Branch) {
    Write-Host "      currently on '$branchNow', expected '$Branch'" -ForegroundColor Yellow
    Write-Host "      run:  git branch -M $Branch" -ForegroundColor Yellow
    Fail "wrong branch. Publish from '$Branch' so the default branch matches GitHub."
}
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "      uncommitted changes present:" -ForegroundColor Yellow
    $dirty | Select-Object -First 10 | ForEach-Object { Write-Host "        $_" }
    Fail "commit or stash them first; publishing a half-saved tree makes the history misleading."
}
$commits = (git rev-list --count HEAD).Trim()
Write-Host "      branch '$Branch' clean, $commits commit(s)" -ForegroundColor Green

# --- 3. Point origin at the right URL -----------------------------------------
if (-not $RemoteUrl) { $RemoteUrl = "https://github.com/$User/$Repo.git" }
Write-Host "[3/5] configuring remote origin -> $RemoteUrl"
$remotes = git remote
if ($remotes -contains 'origin') {
    git remote set-url origin $RemoteUrl
} else {
    git remote add origin $RemoteUrl
}

# --- 4. Warn early if the repository does not exist --------------------------
Write-Host "[4/5] probing the remote (read-only)..."
$env:GIT_TERMINAL_PROMPT = '0'
$probe = git ls-remote --heads origin 2>&1
if ($LASTEXITCODE -ne 0) {
    if ("$probe" -match 'not found|404|Repository not found') {
        Write-Host @"

The remote does not exist yet. Create it first:
  1. open https://github.com/new
  2. Repository name: $Repo
  3. Visibility: Public  (the competition requires an open-source repo)
  4. Do NOT tick "Add a README file", ".gitignore" or "license"
     -- this repository already has commits, and an initialised remote
        makes the first push fail with "rejected (fetch first)".
  5. Create repository, then re-run this script.

"@ -ForegroundColor Yellow
        exit 2
    }
    # Any other probe failure (TLS, credentials, no network) must stop here.
    # Blindly pushing after a failed probe is how you end up staring at a
    # credential prompt wondering whether the command is hung.
    Write-Host "      probe output:" -ForegroundColor Yellow
    "$probe" -split "`n" | Select-Object -First 5 | ForEach-Object { Write-Host "        $_" }
    Fail "could not reach $RemoteUrl. Fix connectivity or credentials, then re-run."
}
$env:GIT_TERMINAL_PROMPT = '1'
Write-Host "      remote reachable" -ForegroundColor Green

# --- 5. Push ------------------------------------------------------------------
Write-Host "[5/5] pushing to origin/$Branch ..."
Write-Host "      (your browser or credential manager may ask you to sign in now)" -ForegroundColor Yellow
git push -u origin $Branch
if ($LASTEXITCODE -ne 0) { Fail "push was rejected. If it says 'fetch first', run: git pull --rebase origin $Branch" }

Write-Host ""
Write-Host "Published: https://github.com/$User/$Repo" -ForegroundColor Green
Write-Host ""
Write-Host "Next, for the team:" -ForegroundColor Cyan
Write-Host "  * Settings -> Collaborators -> add your three teammates"
Write-Host "  * Settings -> Branches -> protect '$Branch': require a pull request + the 'ci' check"
Write-Host "  * never commit .env; keys stay only on the EC2 box"
