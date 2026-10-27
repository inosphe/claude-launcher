# Install claunch as `claunch-nogil`: an editable uv tool on free-threaded
# CPython 3.14t, building zvec from source first (claunch-d18v8). The regular
# `claunch` tool environment is not touched. Windows counterpart of
# tools/install_freethreaded.sh.
#
# PyPI has no cp314t wheel and no sdist for zvec 0.7/0.8, and pyproject.toml caps
# zvec below 0.8, so `uv tool install -e ".[p2p]" --python 3.14t` cannot resolve
# on its own. This script builds a zvec wheel from the upstream git repository
# once, caches it, and installs the project with --overrides pointing zvec at
# that wheel. pyproject.toml stays unchanged.
#
# Usage:
#   pwsh -File tools\install_freethreaded.ps1 [-Rebuild] [-NoP2p | -RequireP2p]
#
# One feature is optional: the fast direct link between the browser page and
# the daemon. Without it the page still works and falls back to the relay
# server, which is slower. Building that link needs a system library that is
# often missing, so the script checks for it first and tells you what to
# install, instead of finding out after the 9-minute build and quietly
# installing without it. The technical name for the feature is "p2p" (the
# `p2p` extra of this package, an extra set of dependencies).
#   -NoP2p       install without that feature, without asking
#   -RequireP2p  stop with exit code 1 when the feature cannot be built
#   (neither)    ask on a terminal; with no terminal (a pipeline, CI, a spawned
#                shell) stop instead of continuing without it
# The script installs without the feature only when -NoP2p or your answer said
# so, and the closing summary states whether it is in.
#
# Environment:
#   CLAUNCH_FT_ZVEC_REF    zvec git ref to build (default: the commit measured in README)
#   CLAUNCH_FT_ZVEC_REPO   zvec git URL (default: https://github.com/alibaba/zvec)
#   CLAUNCH_FT_WHEELS      wheel cache directory (default: %LOCALAPPDATA%\claunch\zvec-wheels,
#                          or <local app data>\claunch\zvec-wheels when that variable is empty)
#   CLAUNCH_FT_ZVEC_WHEEL  path to an existing cp314t zvec wheel; skips the build
#   CLAUNCH_FT_ROOT        tool environment and bin (default: %LOCALAPPDATA%\claunch-nogil,
#                          or <local app data>\claunch-nogil when that variable is empty)
#   CLAUNCH_FT_SHIM_DIR    where claunch-nogil.cmd is written (default: ~/.local/bin)
#
# The tool environment lives in its own UV_TOOL_DIR/UV_TOOL_BIN_DIR under
# CLAUNCH_FT_ROOT, so `uv tool install` cannot replace the `claude-launcher`
# tool of the regular `claunch`. claunch-nogil.cmd sets PYTHON_GIL=0 and runs the
# claunch of that environment. Both commands share ~\.claude-launcher and port
# 8378: one daemon at a time. To go back: `claunch daemon restart`.
#
# Requires "Visual Studio Build Tools" with the C++ workload for the zvec build.
# Building zvec on Windows from this ref has not been run by the author of this
# script; the Linux path (install_freethreaded.sh) has. The part above the zvec
# build -- the p2p decision and its three branches -- does run off Windows under
# pwsh, which is how it is tested (claunch-fsn9f.1).
#
# --reinstall rebuilds the tool environment. --force is only needed when a
# claunch executable that uv did not create sits in the tool bin directory; the
# script does not pass it. The script never starts, stops or restarts the daemon.

param(
    [switch]$Rebuild,
    [switch]$NoP2p,
    [switch]$RequireP2p
)

$ErrorActionPreference = 'Stop'

if ($NoP2p -and $RequireP2p) {
    Write-Host '-NoP2p and -RequireP2p contradict each other; pass one'
    exit 2
}

# Where the per-user defaults live. That is $env:LOCALAPPDATA on Windows; the
# variable is empty on other hosts, and Join-Path refuses a null base, so the
# script used to throw while computing its defaults -- before the p2p decision
# and before saying anything useful, which made the decision branches
# unrunnable anywhere but Windows (claunch-fsn9f.1). GetFolderPath answers on
# every host: it reads LOCALAPPDATA on Windows and the XDG directory elsewhere.
function Get-LocalAppData {
    if ($env:LOCALAPPDATA) { return $env:LOCALAPPDATA }
    $folder = [Environment]::GetFolderPath('LocalApplicationData')
    if ($folder) { return $folder }
    return (Join-Path (Join-Path $HOME '.local') 'share')
}

$LocalAppData = Get-LocalAppData

$ZvecRepo = if ($env:CLAUNCH_FT_ZVEC_REPO) { $env:CLAUNCH_FT_ZVEC_REPO } else { 'https://github.com/alibaba/zvec' }
$ZvecRef = if ($env:CLAUNCH_FT_ZVEC_REF) { $env:CLAUNCH_FT_ZVEC_REF } else { '8d4996924bce78f8b072a353803c305101bcc807' }
$CacheRoot = if ($env:CLAUNCH_FT_WHEELS) { $env:CLAUNCH_FT_WHEELS } else { Join-Path (Join-Path $LocalAppData 'claunch') 'zvec-wheels' }
$PythonSpec = '3.14t'
$FtRoot = if ($env:CLAUNCH_FT_ROOT) { $env:CLAUNCH_FT_ROOT } else { Join-Path $LocalAppData 'claunch-nogil' }
$ShimDir = if ($env:CLAUNCH_FT_SHIM_DIR) { $env:CLAUNCH_FT_SHIM_DIR } else { Join-Path (Join-Path $HOME '.local') 'bin' }
$env:UV_TOOL_DIR = Join-Path $FtRoot 'tools'
$env:UV_TOOL_BIN_DIR = Join-Path $FtRoot 'bin'
New-Item -ItemType Directory -Force -Path $env:UV_TOOL_DIR, $env:UV_TOOL_BIN_DIR, $ShimDir | Out-Null

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Test-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "missing: $Name ($Hint)" }
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
if (-not (Test-Path 'pyproject.toml')) { throw "pyproject.toml not found in $RepoRoot" }

Test-Command 'uv' 'https://docs.astral.sh/uv/'
Test-Command 'git' 'needed to fetch zvec and its submodules'

# One optional feature needs a system library: the fast direct link that lets
# the browser page talk to the daemon without going through the relay server.
# Without it everything still works; the page takes the relay path, which is
# slower. The check happens here — before the nine-minute zvec build — and a
# missing library asks what to do instead of quietly installing without the
# feature (claunch-fsn9f). Windows has no apt line, so the guide shows vcpkg.
function Get-P2pMissing {
    # srtp2.dll anywhere on PATH, or a static srtp2.lib from a vcpkg tree.
    $srtp2 = $null
    foreach ($dir in ($env:PATH -split [IO.Path]::PathSeparator)) {
        if (-not $dir) { continue }
        $candidate = Join-Path $dir 'srtp2.dll'
        if (Test-Path -LiteralPath $candidate) { $srtp2 = $candidate; break }
    }
    if (-not $srtp2 -and $env:SystemDrive) {
        $vcpkgRoot = Join-Path $env:SystemDrive 'vcpkg'
        if (Test-Path -LiteralPath $vcpkgRoot) {
            $srtp2 = Get-ChildItem -Path $vcpkgRoot -Filter 'srtp2.lib' -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
        }
    }
    $hasPkgSrtp2 = $false
    if (Get-Command 'pkg-config' -ErrorAction SilentlyContinue) {
        & pkg-config --exists libsrtp2 2>$null
        $hasPkgSrtp2 = ($LASTEXITCODE -eq 0)
    }
    if (-not ($srtp2 -or $hasPkgSrtp2)) {
        return '이 컴퓨터에 libsrtp2(빠른 통신 기능이 쓰는 암호화 라이브러리)가 없어서, 브라우저와 데몬을 직접 연결하는 기능을 켤 수 없습니다.'
    }
    return ''
}

$Extras = '.[p2p]'
$P2pInstalled = $true
if (-not $NoP2p) {
    $p2pReason = Get-P2pMissing
    if ($p2pReason) {
        Write-Host @"
$p2pReason

무엇이 달라지나요:
  이 설치로 claunch는 그대로 동작합니다. 다만 웹 화면을 열어 둘 때, 브라우저와
  데몬이 서버를 거치지 않고 직접 연결되는 빠른 경로는 쓰지 못하고 중계 서버를
  거치는 느린 경로로 동작합니다. (이 기능의 이름은 p2p입니다.)

고치려면 아래 네 줄을 그대로 실행한 뒤 이 스크립트를 다시 실행하세요
(이미 vcpkg가 있으면 첫 줄은 건너뜁니다. <vcpkg root>는 vcpkg를 설치한 폴더입니다):
    winget install --id Microsoft.vcpkg
    vcpkg install libsrtp2:x64-windows
    `$env:LIB += ";<vcpkg root>\installed\x64-windows\lib"; `$env:INCLUDE += ";<vcpkg root>\installed\x64-windows\include"
    pwsh -File tools\install_freethreaded.ps1 -Rebuild

지금 어떻게 할까요?
  1) 이 기능 없이 설치를 마친다  (나중에 위 명령으로 추가할 수 있습니다)
  2) 여기서 멈춘다  (위 명령을 실행한 뒤 다시 실행하세요)
"@
        if ($RequireP2p) {
            Write-Host '멈춥니다 — -RequireP2p를 주셨습니다.'
            exit 1
        }
        $interactive = $false
        try { $interactive = -not [Console]::IsInputRedirected } catch { $interactive = $false }
        if (-not $interactive) {
            Write-Host '멈춥니다: 여기서는 물어볼 수 없습니다(터미널이 아닌 곳에서 실행 중). 이 기능 없이 설치하려면 -NoP2p를 붙여 다시 실행하세요.'
            exit 1
        }
        $answer = ''
        while ($true) {
            $answer = Read-Host '번호를 고르세요 [1/2] (그냥 Enter: 2, 멈춤)'
            if ($answer -eq '1') { $Extras = '.'; $P2pInstalled = $false; break }
            if ($answer -eq '2' -or $answer -eq '') { Write-Host '멈춥니다.'; exit 1 }
            Write-Host '1 또는 2를 입력하세요.'
        }
    }
} else {
    $Extras = '.'
    $P2pInstalled = $false
}

Invoke-Native 'uv' @('python', 'install', $PythonSpec)

$Wheel = $env:CLAUNCH_FT_ZVEC_WHEEL
if (-not $Wheel) {
    # zvec builds with scikit-build-core; cmake and ninja come from the build
    # requirements. Only the MSVC C++ toolchain has to exist on the machine.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $hasMsvc = (Get-Command cl -ErrorAction SilentlyContinue) -or
        ((Test-Path $vswhere) -and (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath))
    if (-not $hasMsvc) { throw 'missing: MSVC C++ build tools (install "Build Tools for Visual Studio" with the "Desktop development with C++" workload)' }

    $OutDir = Join-Path $CacheRoot $ZvecRef.Substring(0, [Math]::Min(12, $ZvecRef.Length))
    if ($Rebuild -and (Test-Path $OutDir)) { Remove-Item -Recurse -Force $OutDir }
    $found = Get-ChildItem -Path $OutDir -Filter 'zvec-*-cp314t-*.whl' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        $Src = Join-Path ([IO.Path]::GetTempPath()) ("zvec-src-" + [Guid]::NewGuid().ToString('N'))
        try {
            Write-Host "building zvec @ $ZvecRef for $PythonSpec (about 9 minutes on 4+ cores)"
            Invoke-Native 'git' @('clone', '--quiet', '--recursive', $ZvecRepo, $Src)
            Invoke-Native 'git' @('-C', $Src, 'checkout', '--quiet', $ZvecRef)
            Invoke-Native 'git' @('-C', $Src, 'submodule', 'update', '--quiet', '--init', '--recursive')
            Invoke-Native 'uv' @('build', '--wheel', '--python', $PythonSpec, '--out-dir', $OutDir, $Src)
        } finally {
            if (Test-Path $Src) { Remove-Item -Recurse -Force $Src -ErrorAction SilentlyContinue }
        }
        $found = Get-ChildItem -Path $OutDir -Filter 'zvec-*-cp314t-*.whl' | Select-Object -First 1
    }
    $Wheel = $found.FullName
}
if (-not (Test-Path $Wheel)) { throw "zvec wheel not found: $Wheel" }
$Wheel = (Resolve-Path $Wheel).Path
Write-Host "zvec wheel: $Wheel"

$Overrides = Join-Path ([IO.Path]::GetTempPath()) ("claunch-ft-overrides-" + [Guid]::NewGuid().ToString('N') + '.txt')
try {
    $uri = ([Uri]$Wheel).AbsoluteUri
    # ASCII without a byte-order mark: uv reads the file as plain requirements.
    [IO.File]::WriteAllText($Overrides, "zvec @ $uri`n", (New-Object Text.UTF8Encoding($false)))

    function Install-Tool([string]$Target) {
        Invoke-Native 'uv' @('tool', 'install', '--python', $PythonSpec, '--editable', $Target, '--reinstall', '--overrides', $Overrides)
    }

    # No fallback here. p2p was already decided above, so a failure now means
    # this machine differs from what the check reported; downgrading the
    # install on the way out would hide that (claunch-fsn9f). Retry with
    # -NoP2p to choose it.
    Install-Tool $Extras
} finally {
    Remove-Item -Force $Overrides -ErrorAction SilentlyContinue
}

$Shim = Join-Path $ShimDir 'claunch-nogil.cmd'
[IO.File]::WriteAllText($Shim, "@echo off`r`nset PYTHON_GIL=0`r`n`"$(Join-Path $env:UV_TOOL_BIN_DIR 'claunch.exe')`" %*`r`n", (New-Object Text.UTF8Encoding($false)))
Write-Host "wrote $Shim"

$toolDir = (& uv tool dir).Trim()
$toolPython = Join-Path $toolDir 'claude-launcher\Scripts\python.exe'
$env:PYTHON_GIL = '0'
$check = @'
import sys, sysconfig
import zvec
print("python:", sys.version.split()[0], "| Py_GIL_DISABLED =", sysconfig.get_config_var("Py_GIL_DISABLED"))
print("zvec:", getattr(zvec, "__version__", "?"), "| GIL enabled with PYTHON_GIL=0:", sys._is_gil_enabled())
'@
$check | & $toolPython -
if ($LASTEXITCODE -ne 0) { throw 'post-install check failed: zvec did not import in the tool environment' }

$P2pLine = if ($P2pInstalled) {
    '빠른 직접 연결 기능: 포함됨 (브라우저와 데몬이 중계 서버를 거치지 않고 연결될 수 있습니다)'
} else {
    '빠른 직접 연결 기능: 포함되지 않음. 이 설치는 중계 서버를 거치는 느린 경로로 동작합니다.' + [Environment]::NewLine +
    '  이 기능을 추가하려면: vcpkg install libsrtp2:x64-windows 로 라이브러리를 갖춘 뒤 pwsh -File tools\install_freethreaded.ps1 -Rebuild'
}

Write-Host @"

installed as claunch-nogil ($Shim). The regular claunch is unchanged and the
daemon is not restarted by this script. To run the daemon on 3.14t, restart it
yourself with:

    claunch-nogil daemon restart

$P2pLine

Only one daemon can use port 8378. Going back: claunch daemon restart
"@
