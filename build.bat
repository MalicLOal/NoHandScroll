@echo off
setlocal
REM HandScroll one-file build (Windows). Output: dist\HandScroll.exe, log: build_log.txt
cd /d "%~dp0"
set "LOG=%~dp0build_log.txt"
echo [%date% %time%] build start> "%LOG%"

REM stop a running HandScroll so the exe can be overwritten
taskkill /im HandScroll.exe /f >nul 2>&1

py -3.11 -c "import sys;print(sys.version)" >> "%LOG%" 2>&1
if errorlevel 1 goto install_py
goto find_py

:install_py
echo Installing Python 3.11 via winget...
echo Python 3.11 not found, installing via winget>> "%LOG%"
winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1

:find_py
set "PYEXE="
for /f "delims=" %%i in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE (
  echo ERROR: Python 3.11 not available>> "%LOG%"
  echo BUILD_FAILED python>> "%LOG%"
  echo Python 3.11 not found. See build_log.txt
  pause
  exit /b 1
)
echo Using %PYEXE%>> "%LOG%"

set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" "%VPY%" -c "import mediapipe, PyInstaller, pystray, PIL" >nul 2>&1 && goto build
echo Creating venv and installing packages (first time only, takes a few minutes)...
"%PYEXE%" -m venv .venv >> "%LOG%" 2>&1
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  echo BUILD_FAILED pip>> "%LOG%"
  echo Package install failed. See build_log.txt
  pause
  exit /b 1
)

:build

echo Building exe...
"%VPY%" -m PyInstaller --noconfirm --onefile --noconsole --name HandScroll --collect-all mediapipe --exclude-module jax --exclude-module jaxlib --exclude-module scipy --exclude-module torch --exclude-module tensorflow --exclude-module IPython hand_scroll.py >> "%LOG%" 2>&1
if not exist "dist\HandScroll.exe" (
  echo BUILD_FAILED pyinstaller>> "%LOG%"
  echo Build failed. See build_log.txt
  pause
  exit /b 1
)

echo BUILD_OK>> "%LOG%"
echo Build done: dist\HandScroll.exe - launching
start "" "%~dp0dist\HandScroll.exe"
timeout /t 5 >nul
