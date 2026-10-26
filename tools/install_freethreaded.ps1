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
#   pwsh -File tools\install_freethreaded.ps1 [-Rebuild] [-NoP2p]
#
# Environment:
#   CLAUNCH_FT_ZVEC_REF    zvec git ref to build (default: the commit measured in README)
#   CLAUNCH_FT_ZVEC_REPO   zvec git URL (default: https://github.com/alibaba/zvec)
#   CLAUNCH_FT_WHEELS      wheel cache directory (default: %LOCALAPPDATA%\claunch\zvec-wheels)
#   CLAUNCH_FT_ZVEC_WHEEL  path to an existing cp314t zvec wheel; skips the build
#   CLAUNCH_FT_ROOT        tool environment and bin (default: %LOCALAPPDATA%\claunch-nogil)
#   CLAUNCH_FT_SHIM_DIR    where claunch-nogil.cmd is written (default: ~\.local\bin)
#
# The tool environment lives in its own UV_TOOL_DIR/UV_TOOL_BIN_DIR under
# CLAUNCH_FT_ROOT, so `uv tool install` cannot replace the `claude-launcher`
# tool of the regular `claunch`. claunch-nogil.cmd sets PYTHON_GIL=0 and runs the
# claunch of that environment. Both commands share ~\.claude-launcher and port
# 8378: one daemon at a time. To go back: `claunch daemon restart`.
#
# Requires "Visual Studio Build Tools" with the C++ workload for the zvec build.
# Building zvec on Windows from this ref has not been run by the author of this
# script; the Linux path (install_freethreaded.sh) has.
#
# --reinstall rebuilds the tool environment. --force is only needed when a
# claunch executable that uv did not create sits in the tool bin directory; the
# script does not pass it. The script never starts, stops or restarts the daemon.

param(
    [switch]$Rebuild,
    [switch]$NoP2p
)

$ErrorActionPreference = 'Stop'

$ZvecRepo = if ($env:CLAUNCH_FT_ZVEC_REPO) { $env:CLAUNCH_FT_ZVEC_REPO } else { 'https://github.com/alibaba/zvec' }
$ZvecRef = if ($env:CLAUNCH_FT_ZVEC_REF) { $env:CLAUNCH_FT_ZVEC_REF } else { '8d4996924bce78f8b072a353803c305101bcc807' }
$CacheRoot = if ($env:CLAUNCH_FT_WHEELS) { $env:CLAUNCH_FT_WHEELS } else { Join-Path $env:LOCALAPPDATA 'claunch\zvec-wheels' }
$PythonSpec = '3.14t'
$FtRoot = if ($env:CLAUNCH_FT_ROOT) { $env:CLAUNCH_FT_ROOT } else { Join-Path $env:LOCALAPPDATA 'claunch-nogil' }
$ShimDir = if ($env:CLAUNCH_FT_SHIM_DIR) { $env:CLAUNCH_FT_SHIM_DIR } else { Join-Path $HOME '.local\bin' }
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

    if ($NoP2p) {
        Install-Tool '.'
    } else {
        try {
            Install-Tool '.[p2p]'
        } catch {
            # aiortc's native dependencies have no cp314t wheels and may need a
            # compiler toolchain; the daemon runs unchanged without p2p.
            Write-Warning "p2p extra failed to install; installing without it. $_"
            Install-Tool '.'
        }
    }
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

Write-Host @"

installed as claunch-nogil ($Shim). The regular claunch is unchanged and the
daemon is not restarted by this script. To run the daemon on 3.14t, restart it
yourself with:

    claunch-nogil daemon restart

Only one daemon can use port 8378. Going back: claunch daemon restart
"@
