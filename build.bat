@echo off
echo ============================================================
echo   Building YouTube Transcriber - Windows EXE
echo ============================================================
echo.

echo [1/4] Installing build dependencies...
pip install pyinstaller >nul 2>&1

echo [2/4] Downloading yt-dlp.exe...
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe -o yt-dlp.exe
if not exist yt-dlp.exe (
    echo ERROR: Failed to download yt-dlp.exe
    pause
    exit /b 1
)

echo [3/4] Building transcribe.exe with PyInstaller...
pyinstaller --onefile --console --name "YouTube Transcriber" --add-binary "yt-dlp.exe;." transcribe.py

echo [4/4] Copying yt-dlp.exe into dist folder...
copy yt-dlp.exe "dist\yt-dlp.exe" >nul

echo.
echo ============================================================
echo   Done! Your distributable files are in the dist\ folder:
echo.
echo     dist\YouTube Transcriber.exe   <- the main program
echo     dist\yt-dlp.exe                <- required, keep alongside it
echo.
echo   Copy BOTH files to any Windows PC and it will just work.
echo   No Python installation needed on the target PC.
echo ============================================================
pause
