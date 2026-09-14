@echo off
REM Launch the MT5 Grid Bot dashboard (GUI)
cd /d "%~dp0"
start "" py -3.11 gui.py
