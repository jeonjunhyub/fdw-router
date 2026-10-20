# fdw-router 로컬 실행 — 규격 v2.2 전건 시험 + fdw-ops 시뮬레이터 위에서 라우터 배정
# 어느 PC 에서나 돈다: uv 가 있으면 uv, 없으면 설치된 Python 3.10+ 로 가상환경을 만든다.
# 사용: powershell -ExecutionPolicy Bypass -File deploy\run_local.ps1 [-SkipTests] [-RouterOnly] [-NoFetch] [-Recreate]
#   -SkipTests   pytest 생략
#   -RouterOnly  통합 실행에서 원본 FDWOrchestrator 비교 생략
#   -NoFetch     fdw-ops 소스가 없어도 GitHub 에서 받지 않는다
#   -Recreate    .venv 를 지우고 새로 만든다
param(
    [switch]$SkipTests,
    [switch]$RouterOnly,
    [switch]$NoFetch,
    [switch]$Recreate
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Repo        # 경로에 "[04]" 가 있어 와일드카드 해석을 막는다

# 포터블 git / uv (관리자 권한 없이 설치한 위치). 이미 PATH 에 있으면 그것을 쓴다.
$env:Path = "$env:LOCALAPPDATA\Programs\MinGit\cmd;$env:USERPROFILE\.local\bin;" + $env:Path
$env:PYTHONIOENCODING = "utf-8"

# ---------------------------------------------------------------------------
# 1) 규격 정본 (v2.2, fdw.v1.1). 다른 배포본을 쓰려면 FDW_SPEC_ROOT 만 바꾼다.
#    기본 탐색: 저장소 한 단계 위 또는 두 단계 위의 FDW_Y1_DataDefinition_v2.2 폴더
# ---------------------------------------------------------------------------
if (-not $env:FDW_SPEC_ROOT) {
    $specRel = "FDW_Y1_DataDefinition_v2.2"
    foreach ($base in @((Split-Path -Parent (Split-Path -Parent $Repo)), (Split-Path -Parent $Repo))) {
        $cand = Join-Path $base $specRel
        if (Test-Path -LiteralPath $cand) { $env:FDW_SPEC_ROOT = $cand; break }
    }
    if (-not $env:FDW_SPEC_ROOT) { $env:FDW_SPEC_ROOT = Join-Path (Split-Path -Parent (Split-Path -Parent $Repo)) $specRel }
}
if (-not (Test-Path -LiteralPath $env:FDW_SPEC_ROOT)) {
    throw "규격 패키지가 없습니다: $env:FDW_SPEC_ROOT`n  규격 배포본(FDW_Y1_DataDefinition_v2.2)을 받아 그 폴더 경로를 환경변수 FDW_SPEC_ROOT 에 넣고 다시 실행하세요."
}

# ---------------------------------------------------------------------------
# 2) fdw-ops (Isaac Sim 가상 작업장) 소스. 없으면 GitHub 에서 받는다 (-NoFetch 면 건너뛴다).
# ---------------------------------------------------------------------------
if (-not $env:FDW_OPS_ROOT) {
    $env:FDW_OPS_ROOT = Join-Path (Split-Path -Parent $Repo) "external\fdw-ops"
}
if (-not (Test-Path -LiteralPath (Join-Path $env:FDW_OPS_ROOT "fdw_sim")) -and -not $NoFetch) {
    Write-Host "== fdw-ops 소스가 없어 GitHub 에서 받습니다 → $env:FDW_OPS_ROOT"
    & (Join-Path $PSScriptRoot "fetch_external.ps1") -OpsDest $env:FDW_OPS_ROOT -OpsOnly
}

# ---------------------------------------------------------------------------
# 3) Python 가상환경 (.venv)
#    - 다른 PC 에서 만든 .venv 는 기준 Python 경로가 없어 실행이 안 된다 → 감지해서 새로 만든다.
#    - uv 가 있으면 uv (Python 3.11 을 스스로 받는다), 없으면 설치된 Python 3.10+ 의 venv 모듈.
# ---------------------------------------------------------------------------
$venv = Join-Path $Repo ".venv"
$py = Join-Path $venv "Scripts\python.exe"

# 네이티브 명령을 조용히 실행하고 성공 여부만 돌려준다. ($ErrorActionPreference = "Stop" 아래에서는
# 네이티브 명령이 stderr 에 한 줄만 써도 예외가 되어 Python 탐색이 중단되므로, 잠시 Continue 로 내린다.)
function Invoke-Quiet {
    param([string]$Exe, [string[]]$ArgList)
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $Exe @ArgList 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $old }
}

function Test-VenvWorks {
    if (-not (Test-Path -LiteralPath $py)) { return $false }
    $cfg = Join-Path $venv "pyvenv.cfg"
    if (Test-Path -LiteralPath $cfg) {
        $home_ = (Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*', ''
        if ($home_ -and -not (Test-Path -LiteralPath $home_)) { return $false }   # 다른 PC 의 Python 을 가리킨다
    }
    return (Invoke-Quiet $py @("-c", "import sys"))
}

if ($Recreate -or ((Test-Path -LiteralPath $venv) -and -not (Test-VenvWorks))) {
    Write-Host "== 기존 .venv 가 이 PC 에서 동작하지 않거나 -Recreate 지정 → 다시 만듭니다"
    Remove-Item -LiteralPath $venv -Recurse -Force
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { $uv = $uv.Source }

if (-not (Test-Path -LiteralPath $venv)) {
    if ($uv) {
        Write-Host "== uv 로 가상환경 생성 (Python 3.11)"
        & $uv venv $venv --python 3.11
    } else {
        # 설치된 Python 탐색: py 런처(3.11 → 3.12 → 3.10 → 3) → python → python3
        $basePy = $null
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            foreach ($v in @("-3.11", "-3.12", "-3.13", "-3.10", "-3")) {
                if (Invoke-Quiet "py" @($v, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)")) { $basePy = @("py", $v); break }
            }
        }
        if (-not $basePy) {
            foreach ($name in @("python", "python3")) {
                $cmd = Get-Command $name -ErrorAction SilentlyContinue
                if ($cmd) {
                    if (Invoke-Quiet $cmd.Source @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)")) { $basePy = @($cmd.Source); break }
                }
            }
        }
        if (-not $basePy) {
            throw "Python 3.10 이상도 uv 도 없습니다. 둘 중 하나를 설치하세요.`n  - Python: https://www.python.org/downloads/ (설치 시 'Add python.exe to PATH' 체크)`n  - uv:     powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
        }
        Write-Host "== 설치된 Python 으로 가상환경 생성: $($basePy -join ' ')"
        & $basePy[0] @($basePy[1..($basePy.Length-1)] + @("-m", "venv", $venv))
    }
}

Write-Host "== 의존성 설치"
if ($uv) {
    & $uv pip install --python $py -q -r requirements.txt -r requirements-dev.txt paho-mqtt
} else {
    & $py -m pip install -q --disable-pip-version-check -r requirements.txt -r requirements-dev.txt paho-mqtt
}
if ($LASTEXITCODE -ne 0) { throw "의존성 설치 실패" }

Write-Host "`n== Python        = $(& $py -c 'import sys; print(sys.version.split()[0])') ($py)"
Write-Host "== FDW_SPEC_ROOT = $env:FDW_SPEC_ROOT"
Write-Host "== FDW_OPS_ROOT  = $env:FDW_OPS_ROOT`n"

# ---------------------------------------------------------------------------
# 4) 시험 전건
# ---------------------------------------------------------------------------
if (-not $SkipTests) {
    & $py -X utf8 -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "pytest 실패" }
}

# ---------------------------------------------------------------------------
# 5) fdw-ops 위에서 라우터 배정 (원본 FDWOrchestrator 와 KPI 나란히 비교)
# ---------------------------------------------------------------------------
if (Test-Path -LiteralPath (Join-Path $env:FDW_OPS_ROOT "fdw_sim")) {
    $runArgs = @("-X", "utf8", "-m", "fdw_router.integrations.fdwops", "--mode", "discrete")
    if ($RouterOnly) { $runArgs += "--router-only" }
    & $py @runArgs
    if ($LASTEXITCODE -ne 0) { throw "fdw-ops 통합 실행 실패" }
} else {
    Write-Warning "fdw-ops 소스가 없어 통합 실행을 건너뜁니다 ($env:FDW_OPS_ROOT). deploy\fetch_external.ps1 로 받을 수 있습니다."
}
