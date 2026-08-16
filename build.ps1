$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed with exit code $LASTEXITCODE" }
python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name DSAPI-Monitor `
  --icon app_icon.ico `
  --add-data "app_icon_source_v2.png;." `
  --version-file version_info.txt `
  deepseek_usage_monitor.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Get-FileHash -Algorithm SHA256 -LiteralPath '.\dist\DSAPI-Monitor.exe'
