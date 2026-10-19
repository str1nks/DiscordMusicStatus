@echo off
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat

echo [1/3] Installing PyInstaller...
python -m pip install --upgrade pyinstaller || goto :fail

echo [2/3] Building DiscordMusicStatus.exe (console menu)...
python -m PyInstaller --noconfirm --clean --onefile --console --name DiscordMusicStatus --collect-submodules winrt --collect-all pypresence DiscordMusicStatus.py || goto :fail

echo [3/3] Building DiscordMusicStatusBg.exe (background process, no window)...
python -m PyInstaller --noconfirm --onefile --noconsole --name DiscordMusicStatusBg --collect-submodules winrt --collect-all pypresence DiscordMusicStatus.py || goto :fail

echo.
echo Done. Files are in the "dist" folder:
echo   dist\DiscordMusicStatus.exe
echo   dist\DiscordMusicStatusBg.exe
echo Keep both files in the same folder. Run DiscordMusicStatus.exe.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. See the messages above.
pause
exit /b 1
