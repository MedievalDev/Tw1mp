@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat" >nul
pushd "%~dp0"
cl /nologo /EHsc /W3 /I include test_p2p.cpp /link ws2_32.lib ole32.lib /OUT:test_p2p.exe
set rc=%errorlevel%
popd
exit /b %rc%
