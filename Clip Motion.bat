@echo off
rem Clip Motion. Double-clic (sans argument) : ouvre l'interface web.
rem Ligne de commande (PowerShell) : ^& ".\Clip Motion.bat" -h
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Clip Motion
"%~dp0comfyui\ComfyUI_windows_portable\python_embeded\python.exe" -s -I "%~dp0outils\clipmotion.py" %*
if errorlevel 1 if "%~1"=="" pause
