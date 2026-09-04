@echo off
echo ========================================================
echo Push Local Repository to Your GitHub Account
echo ========================================================
echo.
echo Please create a blank repository on GitHub first.
echo Then, paste the HTTPS or SSH URL below.
echo (Example: https://github.com/your-username/migration-platform.git)
echo.

set /p GITHUB_URL="Enter your GitHub URL: "

if "%GITHUB_URL%"=="" (
    echo Error: No URL provided.
    pause
    exit /b
)

echo.
echo Linking local repository to: %GITHUB_URL%
REM Remove existing origin just in case one was already set
git remote remove origin 2>nul
git remote add origin %GITHUB_URL%

echo.
echo Pushing all branches (main, develop, feature) to GitHub...
git push -u origin --all

echo.
echo ========================================================
echo Done! Your code should now be visible on GitHub.
echo ========================================================
pause
