<#
.SYNOPSIS
    Vibe Excel（Python 版）启动脚本。

.DESCRIPTION
    默认：把 store 渲染成站点并打开索引页。
    -Full   从零跑一轮：setup → run → publish，然后出站点（只接一个 store）。
    -Status 打印每个 store 的当前状态，不生成任何文件。
    -Test   跑测试。

    不给 -Store 时，脚本会在 -Root 下按候选名找 store（见下方 $candidates），
    找到几个就合并成几行项目列在首页。

.EXAMPLE
    .\start.ps1

.EXAMPLE
    .\start.ps1 -Store C:\project\vibe_excel-store -NoOpen

.EXAMPLE
    .\start.ps1 -Full -Store C:\project\_vibe-demo\store
#>
[CmdletBinding()]
param(
    # 数据根目录。留空则取仓库的上一级（store 通常与仓库并排放）
    [string]$Root,
    # 显式指定 store，可给多个
    [string[]]$Store = @(),
    [string]$Inputs,
    [string]$Out,
    [string]$AsOf = "2026-11-11",
    [switch]$Full,
    [switch]$Status,
    [switch]$Test,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"

if (-not $Root) {
    $Root = Split-Path $PSScriptRoot -Parent
}

# 输出用 UTF-8，避免中文在控制台变成乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# 别的项目留下的 venv 会让工具走错解释器
$env:VIRTUAL_ENV = $null

function Write-Step([string]$Text) { Write-Host ""; Write-Host "── $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "   $Text" -ForegroundColor DarkGray }
function Fail([string]$Text) { Write-Host ""; Write-Host $Text -ForegroundColor Red; exit 1 }

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$vibectl = Join-Path $PSScriptRoot ".venv\Scripts\vibectl.exe"
if (-not (Test-Path $python)) {
    Fail "找不到虚拟环境：$python`n先在本目录跑一次：uv sync --frozen --offline --group dev"
}

# 没显式给 -Store 时，在 -Root 下按这几个名字找（带 control.db 才算），换机器改这里
if (-not $Store) {
    $candidates = @(
        (Join-Path $Root "vibe_excel-store"),
        (Join-Path $Root "store"),
        (Join-Path $Root "_vibe-demo\store")
    )
    $Store = @($candidates | Where-Object { Test-Path (Join-Path $_ "control.db") })
}
if (-not $Store) {
    Fail "在 $Root 下没找到 store。`n用 -Store <store 目录> 指定，或用 -Root <数据目录> 换个位置找。"
}

$Inputs = if ($Inputs) { $Inputs } else { Join-Path $Root "inputs" }
$Out = if ($Out) { $Out } else { Join-Path $Root "site" }

$env:VIBE_EXCEL_PYTHON = $python
$env:VIBE_EXCEL_GROUND_TRUTH = $Inputs
$env:VIBE_EXCEL_AS_OF = $AsOf

Write-Host "Vibe Excel · Python 版" -ForegroundColor White
foreach ($item in $Store) { Write-Note "store  $item" }
Write-Note "site   $Out"

if ($Test) {
    Write-Step "测试"
    & $python -m pytest -q
    exit $LASTEXITCODE
}

if ($Status) {
    foreach ($item in $Store) {
        Write-Step "状态 · $(Split-Path $item -Leaf)"
        $env:VIBE_EXCEL_STORE = $item
        & $vibectl status
    }
    exit 0
}

if ($Full) {
    if ($Store.Count -gt 1) {
        Fail "-Full 只接一个 store：它要往 store 里写数据，不能一次写多个。"
    }
    if (-not (Test-Path $Inputs)) {
        Fail "找不到输入目录：$Inputs`nsetup 需要它下面有清单里写的输入文件。"
    }
    $env:VIBE_EXCEL_STORE = $Store[0]
    foreach ($step in @("setup", "run", "publish")) {
        Write-Step $step
        & $vibectl $step
        if ($LASTEXITCODE -ne 0) {
            Fail "$step 失败（退出码 $LASTEXITCODE），后面的步骤不再继续。"
        }
    }
}

Write-Step "生成站点"
$storeArgs = @()
foreach ($item in $Store) { $storeArgs += @("--store", $item) }
& $python -m vibe_excel.book build @storeArgs --out $Out
if ($LASTEXITCODE -ne 0) {
    Fail "站点生成失败（退出码 $LASTEXITCODE）"
}

$index = Join-Path $Out "index.html"
if (-not (Test-Path $index)) {
    Fail "生成完了却没有 index.html：$index"
}
if (-not $NoOpen) {
    Start-Process $index
    Write-Note "已在浏览器打开 $index"
}
