@echo off
setlocal
set "PWE_TARGET=%APPDATA%\Adobe\CEP\extensions\org.pngwebmexporter.ae"
if exist "%PWE_TARGET%" rmdir /s /q "%PWE_TARGET%"
echo PNG to WebM was removed. Restart After Effects.
if not defined PWE_NO_PAUSE pause
