# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 rodavrow
#
# venv.ps1 - create (first run) and activate the Python venv for bp3_bdm.py
#
# Must be DOT-SOURCED so the activation sticks in your shell:
#     . .\venv.ps1
#
# First run: creates .venv next to this script and installs pyserial.
# Later runs: just activates it.

# Refuse to run as a child script - activation would be lost when it exits.
if ($MyInvocation.InvocationName -ne '.') {
    Write-Error "Run this with:  . '$($MyInvocation.MyCommand.Path)'"
    return
}

$gn30Dir = $PSScriptRoot
$gn30Venv = Join-Path $gn30Dir '.venv'
$gn30Activate = Join-Path $gn30Venv 'Scripts\Activate.ps1'

if (-not (Test-Path $gn30Activate)) {
    # Prefer the py launcher (always present with python.org installs); fall back to python.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $gn30Python = @('py', '-3')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $gn30Python = @('python')
    } else {
        Write-Error "Python 3 not found. Install it from https://www.python.org/downloads/"
        return
    }

    Write-Host "Creating virtual environment in $gn30Venv ..."
    & $gn30Python[0] $gn30Python[1..($gn30Python.Count - 1)] -m venv $gn30Venv
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create venv."
        return
    }

    . $gn30Activate
    python -m pip install --upgrade pip | Out-Null
    python -m pip install pyserial
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install failed; removing incomplete venv."
        deactivate
        Remove-Item -Recurse -Force $gn30Venv
        return
    }
    Write-Host "Venv ready."
} else {
    . $gn30Activate
}

Write-Host "Activated: $env:VIRTUAL_ENV  ($(python --version 2>&1))"

Remove-Variable gn30Dir, gn30Venv, gn30Activate, gn30Python -ErrorAction SilentlyContinue
