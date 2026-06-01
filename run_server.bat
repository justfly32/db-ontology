@echo off
chcp 65001 >nul
title DB Ontology Analyzer
cd /d "%~dp0"
echo DB Ontology Analyzer - API Server
echo ==================================
echo.
echo Open http://localhost:8000/docs in your browser
echo.
uvicorn src.api.server:app --host 127.0.0.1 --port 8000 --reload
pause
