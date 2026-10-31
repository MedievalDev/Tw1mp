@echo off
rem Build the three-peer self-test (host + two joiners in one process against
rem a running relay; see tests/test_relay_dll.py which starts the relay).
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat" >nul
if errorlevel 1 (echo vcvars32 nicht gefunden & exit /b 1)
pushd "%~dp0"
cl /nologo /EHsc /W3 /I include test_relay.cpp /link ws2_32.lib ole32.lib /OUT:test_relay.exe
set rc=%errorlevel%
popd
exit /b %rc%
