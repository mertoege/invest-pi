@echo off
echo === Invest-Pi: Push all new features to GitHub ===
echo.

REM 1. Remove stale lock file
if exist ".git\index.lock" (
    del ".git\index.lock"
    echo Removed stale .git/index.lock
)

REM 2. Stage all changes
git add -A
echo Staged all changes.

REM 3. Commit
git commit -m "feat: complete self-learning + trading optimization suite" -m "New: FRED macro signals, market breadth, news sentiment, earnings calendar," -m "regime-outcome tracking, config patches, dynamic weights, drift detection," -m "correlation sizing, feedback injection, weekly recap, weight optimizer." -m "Enhanced: risk_scorer 3-source macro, calibration blocks, run_strategy" -m "auto-patches, meta_review structured output."

REM 4. Push
git push origin main

echo.
echo === Done! Pi will auto-pull within 5 minutes. ===
pause
