@echo off
REM ==========================================================================
REM  run_all_reports.bat — run the full migration pipeline and every report.
REM
REM  Order matters:
REM    1. app\main.py                    pipeline: validation reports per model
REM                                      type + Pass_Fail_Summary.xlsx
REM    2. migration_audit.py             provenance audit LAST, so its ledger
REM                                      sees everything the others produced
REM
REM  Run it from the project root (the folder containing this file).
REM ==========================================================================
cd /d "%~dp0"

echo.
echo [1/2] Pipeline (validation reports)...
python app\main.py
if errorlevel 1 echo    ^> pipeline reported failures - see output above.

echo.
echo [2/2] Provenance audit...
python app\reporting\migration_audit.py

echo.
echo ==========================================================================
echo  All reports generated:
echo    app\reporting\ldm_reports\[model_name].xlsx
echo    app\reporting\cdm_reports\[model_name].xlsx
echo    app\reporting\pdm_reports\[model_name].xlsx
echo    batch_summary\summary_report\Pass_Fail_Summary.xlsx
echo    batch_summary\audit\migration_audit_report.xlsx
echo ==========================================================================
pause
