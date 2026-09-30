$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name "快速知识查询" `
  --distpath dist `
  --workpath build `
  --specpath . `
  app.py

Write-Host "构建完成：$PSScriptRoot\dist\快速知识查询.exe"
