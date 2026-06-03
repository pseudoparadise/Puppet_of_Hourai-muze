# 家模式 — 在终端运行: .\家.ps1
# 或者右键 → 使用 PowerShell 运行

$env:ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
$env:ANTHROPIC_AUTH_TOKEN="sk-bf690a8cd933477695fcdeee8577f8ba"
$env:ANTHROPIC_MODEL="deepseek-v4-pro[1m]"
$env:ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro[1m]"
$env:ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro[1m]"
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash[1m]"
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
$env:CLAUDE_CODE_EFFORT_LEVEL="max"
$env:PYTHONIOENCODING="utf-8"
$env:GHOST_MODE="home"
Set-Content -Path "$PSScriptRoot\.mode" -Value "home" -Encoding UTF8

Write-Host '家就绪 | mode=home | DSphantom + 全类别检索' -ForegroundColor Magenta
Write-Host '现在启动 claude 即可' -ForegroundColor Gray