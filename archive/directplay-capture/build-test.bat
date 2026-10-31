@echo off
rem Build the self-test that drives the capture shim over the real dpnet.dll
rem without the game (host a session, send to yourself, close).
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat" >nul
if errorlevel 1 (echo vcvars32 nicht gefunden & exit /b 1)
pushd "%~dp0"
cl /nologo /EHsc /W3 /I include test_capture.cpp /link ole32.lib /OUT:test_capture.exe
set rc=%errorlevel%
popd
exit /b %rc%
