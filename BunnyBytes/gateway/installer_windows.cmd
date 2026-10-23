@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Rocsy installer for Windows (untested)
rem Rocsy on Windows - UNTESTED. Detects Windows 7, 8, 8.1, 10 and 11 from `ver`.
rem  Windows 10 version 2004 (build 19041) or newer, and Windows 11: Rocsy runs inside WSL2
rem  (Ubuntu). This sets up WSL2, systemd and Docker Engine in the distro as root
rem  (installer\wsl-setup.sh), then runs the Linux installer there with --windows.
rem  Windows 7, 8, 8.1 and older Windows 10 can't run WSL2: this only adds a shortcut to a
rem  Rocsy server elsewhere on the network.
rem Options go on to the Linux installer: --terminal, --defaults, --dir PATH, --verbose.
rem ROCSY_WSL_DISTRO picks another WSL distro than Ubuntu-24.04 / Ubuntu / Ubuntu-22.04.
cd /d "%~dp0"

set "WINVER="
for /f "tokens=2 delims=[]" %%v in ('ver') do set "WINVER=%%v"
set "MAJOR=0"
set "MINOR=0"
set "BUILD=0"
for /f "tokens=2-4 delims=. " %%a in ("%WINVER%") do set "MAJOR=%%a" & set "MINOR=%%b" & set "BUILD=%%c"
set "WINNAME=Windows %MAJOR%.%MINOR%"
set "WSL2=0"
if "%MAJOR%.%MINOR%"=="6.1" set "WINNAME=Windows 7"
if "%MAJOR%.%MINOR%"=="6.2" set "WINNAME=Windows 8"
if "%MAJOR%.%MINOR%"=="6.3" set "WINNAME=Windows 8.1"
if not "%MAJOR%"=="10" goto :classified
set "WINNAME=Windows 10"
if %BUILD% GEQ 22000 set "WINNAME=Windows 11"
if %BUILD% GEQ 19041 set "WSL2=1"
:classified

echo.
echo   Rocsy installer for Windows
echo   Windows support is UNTESTED: expect rough edges, and keep install.log for bug reports.
echo   Detected: %WINNAME% - version %MAJOR%.%MINOR%, build %BUILD%
echo.
if "%WSL2%"=="1" goto :wsl

rem ---- Windows 7 / 8 / 8.1 / Windows 10 before 2004: open a Rocsy server elsewhere ----
if "%MAJOR%"=="10" goto :old10
echo   %WINNAME% can't run WSL2 or Docker, so Rocsy can't run on this PC.
goto :client
:old10
echo   This Windows 10 build is older than version 2004 - build 19041 - the first with WSL2,
echo   which Rocsy needs. Update Windows, then run this installer again to install Rocsy here.
:client
echo   This PC can still open Rocsy running on another computer on your network
echo   - Linux, or Windows 10/11 - in a browser. Rocsy needs a current browser:
echo   Firefox 115 ESR is the last Firefox for Windows 7, 8 and 8.1.
echo.
:client_ask
set "ROCSY_URL="
set /p "ROCSY_URL=  Rocsy server address, for example http://my-server:8080 - or Enter to quit: "
if not defined ROCSY_URL goto :nothing
powershell -NoProfile -Command "exit [int](-not ($env:ROCSY_URL -match '^https?://[A-Za-z0-9.:_/-]+$'))" >nul 2>&1
if errorlevel 1 (
  echo   Enter an address that starts with http:// or https://
  goto :client_ask
)
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESK=%%d"
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Programs')"`) do set "PROGS=%%d"
if not defined DESK set "DESK=%USERPROFILE%\Desktop"
for %%f in ("%DESK%" "%PROGS%") do if not "%%~f"=="" if exist "%%~f\" (
  > "%%~f\Rocsy.url" echo [InternetShortcut]
  >> "%%~f\Rocsy.url" echo URL=%ROCSY_URL%
  echo   Shortcut: %%~f\Rocsy.url
)
start "" "%ROCSY_URL%"
echo.
echo   Done. Rocsy itself wasn't installed on this PC.
goto :finish

rem ---- Windows 10 (2004+) / 11: Rocsy inside WSL2 ----
:wsl
echo   Rocsy runs inside WSL2, a Linux distro on Windows, with Docker Engine there.
echo   The AMD GPU needs the current AMD Windows driver, and AMD's ROCm for WSL in the distro.
echo.
where wsl.exe >nul 2>&1 || goto :need_wsl
wsl.exe --status >nul 2>&1 || goto :need_wsl

:pick
set "DISTRO=%ROCSY_WSL_DISTRO%"
if defined DISTRO goto :have_distro
for %%d in (Ubuntu-24.04 Ubuntu Ubuntu-22.04) do (
  if not defined DISTRO wsl.exe -d %%d --exec true >nul 2>&1 && set "DISTRO=%%d"
)
if defined DISTRO goto :have_distro
echo   No Ubuntu distro was found in WSL.
choice /c YN /m "  Install Ubuntu 24.04 in WSL now? It asks you for a new Linux user name and password"
if errorlevel 2 goto :nothing
wsl.exe --install -d Ubuntu-24.04
echo.
echo   If Ubuntu opened a Linux prompt, type exit there to continue.
wsl.exe -d Ubuntu-24.04 --exec true >nul 2>&1 || goto :restart_needed
set "DISTRO=Ubuntu-24.04"

:have_distro
echo   WSL distro: %DISTRO%
wsl.exe -d %DISTRO% --exec sh -c "uname -r | grep -qi wsl2" && goto :is_wsl2
echo   Converting %DISTRO% to WSL2 - this can take a few minutes...
wsl.exe --set-version %DISTRO% 2 || goto :fail

:is_wsl2
set "WSLUSER="
for /f "usebackq delims=" %%u in (`wsl.exe -d %DISTRO% --exec id -un`) do set "WSLUSER=%%u"
if not defined WSLUSER goto :fail
wsl.exe -d %DISTRO% -u root --cd "%~dp0." --exec sh ./installer/wsl-setup.sh systemd
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :systemd_on
if not "%RC%"=="3" goto :fail
echo   Turned on systemd in %DISTRO%; restarting the distro...
wsl.exe --terminate %DISTRO%
wsl.exe -d %DISTRO% -u root --exec sh -c "sleep 3; test -d /run/systemd/system" || goto :no_systemd
:systemd_on

echo.
echo   Next, as root inside %DISTRO%: Python, git, Docker Engine, Compose and Buildx - where
echo   missing - Docker access for %WSLUSER%, and lingering user services so Rocsy's helpers
echo   keep running without an open terminal.
choice /c YN /m "  Continue"
if errorlevel 2 goto :nothing
wsl.exe -d %DISTRO% -u root --cd "%~dp0." --exec sh ./installer/wsl-setup.sh packages %WSLUSER% || goto :fail

echo.
echo   Starting the Rocsy installer inside %DISTRO%. Keep this window open until it finishes.
wsl.exe -d %DISTRO% --cd "%~dp0." --exec env ROCSY_WINDOWS=%MAJOR%.%MINOR%.%BUILD% sh "./Install Rocsy.sh" --windows %*
goto :finish

:need_wsl
echo   WSL2 isn't set up on this PC yet.
choice /c YN /m "  Install WSL2 and Ubuntu 24.04 now? It needs administrator rights and a restart"
if errorlevel 2 goto :nothing
powershell -NoProfile -Command "Start-Process wsl.exe -ArgumentList '--install','-d','Ubuntu-24.04' -Verb RunAs -Wait"
:restart_needed
echo.
echo   Restart Windows if it asks you to. When Ubuntu has set up your Linux user name and
echo   password, open "Install Rocsy (Windows).cmd" again.
goto :finish

:no_systemd
echo   systemd didn't start in %DISTRO%. Run "wsl --update" and "wsl --shutdown", then run
echo   this installer again.
goto :finish

:fail
echo.
echo   Setup stopped because a step failed; see the messages above.
goto :finish

:nothing
echo   Nothing was installed.

:finish
echo.
pause
endlocal
