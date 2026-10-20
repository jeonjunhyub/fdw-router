# 외부 소스 받기 — fdw-ops (Isaac Sim 가상 작업장, 공개 저장소 opt1064/fdw-ops).
# 사용: powershell -ExecutionPolicy Bypass -File deploy\fetch_external.ps1 [-OpsDest <경로>] [-Update]
#   기본 목적지: <저장소>\..\external\fdw-ops
#   -Update  이미 받은 fdw-ops 가 git 저장소면 같은 브랜치 최신으로 당긴다
param(
    [string]$OpsDest,
    [switch]$Update,
    [string]$OpsRepo   = "https://github.com/opt1064/fdw-ops.git",
    [string]$OpsBranch = "fix/fdw-quality-disposition-base-pose"
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$External = Join-Path (Split-Path -Parent $Repo) "external"
if (-not $OpsDest)       { $OpsDest       = Join-Path $External "fdw-ops" }
$env:Path = "$env:LOCALAPPDATA\Programs\MinGit\cmd;" + $env:Path

# ---------------------------------------------------------------------------
# fdw-ops
# ---------------------------------------------------------------------------
if ($true) {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath (Join-Path $OpsDest "fdw_sim")) {
        if ($Update -and $git -and (Test-Path -LiteralPath (Join-Path $OpsDest ".git"))) {
            Write-Host "== fdw-ops 갱신: $OpsDest ($OpsBranch)"
            & $git.Source -C $OpsDest fetch -q --depth 1 origin $OpsBranch
            & $git.Source -C $OpsDest checkout -q -B $OpsBranch FETCH_HEAD
        } else {
            Write-Host "== fdw-ops 이미 있음: $OpsDest (갱신하려면 -Update)"
        }
    } else {
        if ($git) {
            Write-Host "== fdw-ops 받기: $OpsRepo ($OpsBranch) → $OpsDest"
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OpsDest) | Out-Null
            & $git.Source clone -q --depth 1 -b $OpsBranch $OpsRepo $OpsDest
        } else {
            # git 이 없으면 GitHub 의 브랜치 zip 을 받아 푼다
            $zipUrl = ($OpsRepo -replace '\.git$', '') + "/archive/refs/heads/" + $OpsBranch + ".zip"
            $tmp = Join-Path $env:TEMP ("fdw-ops-" + [guid]::NewGuid().ToString("N") + ".zip")
            Write-Host "== git 이 없어 zip 으로 받습니다: $zipUrl"
            Invoke-WebRequest -Uri $zipUrl -OutFile $tmp -UseBasicParsing
            $tmpDir = $tmp -replace '\.zip$', ''
            Expand-Archive -LiteralPath $tmp -DestinationPath $tmpDir -Force
            $inner = Get-ChildItem -LiteralPath $tmpDir -Directory | Select-Object -First 1
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OpsDest) | Out-Null
            Move-Item -LiteralPath $inner.FullName -Destination $OpsDest
            Remove-Item -LiteralPath $tmp, $tmpDir -Recurse -Force
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $OpsDest "fdw_sim"))) { throw "fdw-ops 를 받지 못했습니다: $OpsDest" }
    if (Test-Path -LiteralPath (Join-Path $OpsDest ".git")) {
        $head = & git -C $OpsDest log -1 --format='%h %ad %s' --date=short
        Write-Host "   fdw-ops HEAD: $head"
    }
}
