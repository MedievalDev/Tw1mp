@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat" >nul
if errorlevel 1 (echo vcvars32 nicht gefunden & exit /b 1)
pushd "%~dp0"
cl /nologo /LD /O2 /EHsc /W3 /I include dpnetreplace.cpp ^
   /link /OUT:dpnetreplace.dll /DEF:dpnetreplace.def ole32.lib user32.lib
set rc=%errorlevel%
popd
if %rc% neq 0 (echo BUILD FEHLGESCHLAGEN & exit /b %rc%)
echo BUILD OK: %~dp0dpnetreplace.dll
