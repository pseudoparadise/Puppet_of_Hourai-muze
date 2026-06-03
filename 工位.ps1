# 工位模式 — 在终端运行: .\工位.ps1
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
$env:GHOST_MODE="work"
Set-Content -Path "$PSScriptRoot\.mode" -Value "work" -Encoding UTF8

Write-Host '工位就绪 | mode=work | 八荣八耻 + todo/commitments 检索' -ForegroundColor Green
Write-Host '现在启动 claude 即可' -ForegroundColor Gray