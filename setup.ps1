# 实时翻译字幕 — 一键安装
# 用法：右键"用 PowerShell 运行"，或者
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== 1/4 检查 Python" -ForegroundColor Cyan
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "没找到 python，请先安装 Python 3.10+ 并加入 PATH" }
& $py --version

Write-Host "== 2/4 安装依赖（阿里云镜像）" -ForegroundColor Cyan
& $py -m pip install --no-input -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

Write-Host "== 3/4 下载模型（约 2.4GB，从 ModelScope）" -ForegroundColor Cyan
& $py -u download_models.py
& $py -u get_qwen.py

Write-Host "== 4/4 下载 llama.cpp 二进制" -ForegroundColor Cyan
$tmp = Join-Path $env:TEMP "llamacpp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$tag = "b11887"
$base = "https://github.com/ggml-org/llama.cpp/releases/download/$tag/"
foreach ($pair in @(@("llama-$tag-bin-win-vulkan-x64.zip", "llama_vk"),
                    @("llama-$tag-bin-win-cpu-x64.zip", "llama"))) {
    $zip = Join-Path $tmp $pair[0]
    if (-not (Test-Path $zip)) {
        Write-Host "  下载 $($pair[0])"
        Invoke-WebRequest -Uri ($base + $pair[0]) -OutFile $zip -TimeoutSec 300
    }
    Expand-Archive -Path $zip -DestinationPath $pair[1] -Force
}

Write-Host ""
Write-Host "装好了。启动：python app.py" -ForegroundColor Green
Write-Host "纯文字模式：python app.py --minimal" -ForegroundColor Green
