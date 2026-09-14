@echo off
rem Allow port 4680 and Python through Windows Defender Firewall for LAN access
setlocal

>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo Requesting Administrator privileges...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )
    pushd "%CD%"
    CD /D "%~dp0"

echo ========================================================
echo   Configuring Windows Firewall for Jukebox...
echo ========================================================
echo.

echo [1/2] Adding inbound rule for TCP port 4680 (Domain, Private, Public)...
netsh advfirewall firewall delete rule name="Jukebox Web (4680)" >nul 2>nul
netsh advfirewall firewall add rule name="Jukebox Web (4680)" dir=in action=allow protocol=TCP localport=4680 profile=any

echo.
echo [2/2] Updating Python firewall rule profile to ANY...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-NetFirewallRule -DisplayName 'python.exe' -Profile Any -ErrorAction SilentlyContinue" >nul 2>nul

echo.
echo ========================================================
echo   [SUCCESS] Port 4680 is now allowed through firewall!
echo   LAN devices can now access:
echo   http://192.168.10.162:4680
echo ========================================================
echo.
pause
