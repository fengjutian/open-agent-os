#!/usr/bin/env pwsh
# 统一启动脚本（Windows）。
# 用法：.\scripts\dev.ps1 [--executor] [--backend-port 8000] ...
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
if ($null -eq $Rest) { $Rest = @() }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) {
    throw "找不到 Python，请先安装 Python 3.12+ 并加入 PATH"
}

& $python.Source (Join-Path $PSScriptRoot "dev.py") @Rest
exit $LASTEXITCODE
