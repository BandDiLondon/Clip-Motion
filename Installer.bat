@echo off
rem Installation de Clip Motion : double-clic. Options : Installer.bat -Oui / -Verifier / -SansModeles (voir installer.ps1)
chcp 65001 >nul
title Installation de Clip Motion
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer.ps1" %*
set "CODE=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %CODE%
