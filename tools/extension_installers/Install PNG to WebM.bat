@echo off
rem Installs the PNG to WebM After Effects panel for the current user.
setlocal
set "PWE_ZXP=%~dp0PNG-to-WebM-AE.zxp"
set "PWE_TARGET=%APPDATA%\Adobe\CEP\extensions\org.pngwebmexporter.ae"
if exist "%PWE_TARGET%" rmdir /s /q "%PWE_TARGET%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
  "[System.IO.Compression.ZipFile]::ExtractToDirectory($env:PWE_ZXP, $env:PWE_TARGET);" ^
  "Get-ChildItem -LiteralPath $env:PWE_TARGET -Recurse -File | Unblock-File"
if errorlevel 1 (
  echo Installation failed.
  if not defined PWE_NO_PAUSE pause
  exit /b 1
)
echo PNG to WebM is installed.
echo Restart After Effects, then open Window ^> Extensions ^> PNG to WebM.
if not defined PWE_NO_PAUSE pause
