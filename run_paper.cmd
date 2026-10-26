@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%~dp0tools"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PAPER_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PAPER_PYTHON%" (
  echo Create .venv and install the requirements from README.md first.
  exit /b 1
)
if "%~1"=="verify" goto verify
if "%~1"=="plan" goto plan
if "%~1"=="example" goto example
if "%~1"=="test" goto test
echo Usage: run_paper.cmd verify ^| plan ^| example [--program NAME] ^| test
exit /b 0
:verify
"%PAPER_PYTHON%" -B scripts\verify_release.py
exit /b %ERRORLEVEL%
:plan
"%PAPER_PYTHON%" -B tools\run_review_controls_v4_contract_v2.py plan
exit /b %ERRORLEVEL%
:example
"%PAPER_PYTHON%" -B scripts\replay_example.py %2 %3
exit /b %ERRORLEVEL%
:test
"%PAPER_PYTHON%" -B scripts\run_tests.py
exit /b %ERRORLEVEL%
