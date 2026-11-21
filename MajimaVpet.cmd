@echo off
rem Start Majima (Windows) with the Python in program\venv, without a console window
start "" "%~dp0program\venv\Scripts\pythonw.exe" "%~dp0program\vpet_win.py"
