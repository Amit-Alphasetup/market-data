<#
D6 phase 2 — one-time seed of the eod2-data mirror repo.

Run this ONCE, after Amit-Alphasetup/eod2-data exists (private, created by Apd) and your local git
credential has push access to it (the same credential that already pushes market-data/alphadesk should
work, since it is scoped to the Amit-Alphasetup account rather than a single repo - verify with a
`git ls-remote` first if unsure).

What it does: clones the empty mirror repo, copies C:\dev\eod2\src\eod2_data (448 MB: daily/, meta.json,
isin.csv, isin_symbol_map.json, market_tracker.csv, special_sessions.txt - everything dataops export
reads) into it, commits, and pushes. It does not touch C:\dev\eod2 - this is a read-only copy out.

Usage:  powershell -ExecutionPolicy Bypass -File scripts\seed_eod2_data_mirror.ps1
#>

param(
    [string]$MirrorRepoUrl = "https://github.com/Amit-Alphasetup/eod2-data.git",
    [string]$SourceEod2Data = "C:\dev\eod2\src\eod2_data",
    [string]$WorkDir = "$env:TEMP\eod2-data-mirror-seed"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $SourceEod2Data)) {
    throw "Source not found: $SourceEod2Data"
}

if (Test-Path $WorkDir) {
    Remove-Item -Recurse -Force $WorkDir -Confirm:$false
}

Write-Host "Cloning $MirrorRepoUrl ..."
git clone $MirrorRepoUrl $WorkDir
if ($LASTEXITCODE -ne 0) { throw "clone failed - does the repo exist yet, and do you have push access?" }

$existing = Get-ChildItem $WorkDir -Force | Where-Object { $_.Name -ne ".git" }
if ($existing) {
    throw "eod2-data is not empty ($($existing.Count) entries) - refusing to overwrite. This script is for the initial seed only."
}

Set-Content -Path "$WorkDir\.gitattributes" -Value "* text=auto eol=lf" -Encoding utf8

Write-Host "Copying $SourceEod2Data -> $WorkDir (this is the 448 MB step, it takes a while) ..."
robocopy $SourceEod2Data $WorkDir /E /NFL /NDL /NJH /NJS /NC /NS
# robocopy exit codes 0-7 are all success (see MS docs); 8+ is a real failure
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }

Push-Location $WorkDir
try {
    # A freshly cloned repo has no git identity of its own even when other repos on this machine do -
    # set it repo-local so `git commit` below doesn't fail with "Author identity unknown".
    git config user.name "Amit-Alphasetup"
    git config user.email "apdash95@gmail.com"

    git add -A
    $fileCount = (git status --porcelain | Measure-Object -Line).Lines
    $today = Get-Date -Format "yyyy-MM-dd"
    git commit -m "Initial seed: EOD2 data archive as of $today ($fileCount files)"
    if ($LASTEXITCODE -ne 0) { throw "commit failed" }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw "push failed" }
    Write-Host "Seeded. Verify: git ls-remote $MirrorRepoUrl"
}
finally {
    Pop-Location
}
