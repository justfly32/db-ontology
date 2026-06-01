@echo off
chcp 65001 >nul
title DB Ontology Analyzer - CLI
cd /d "%~dp0"
echo DB Ontology Analyzer - CLI Mode
echo =================================
echo.
python main.py
pause
