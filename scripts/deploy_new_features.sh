#!/bin/bash
# deploy_new_features.sh — Run on Pi after git pull
# Usage: ssh pi@100.92.115.43 "cd /home/investpi/invest-pi && bash scripts/deploy_new_features.sh"
set -e

echo "=== Invest-Pi: Deploy New Features ==="

# 1. Init new DB tables (regime_snapshots, config_patch_log)
echo "1. Initializing new DB tables..."
python3 -c "
import sys; sys.path.insert(0, '.')
from src.common.storage import init_all
init_all()
print('   DB init OK')
"

# 2. Install new dependencies
echo "2. Installing dependencies..."
pip3 install --break-system-packages vaderSentiment 2>/dev/null || echo "   vaderSentiment already installed or pip not available"
pip3 install --break-system-packages fredapi 2>/dev/null || echo "   fredapi already installed or pip not available"

# 3. Syntax check all new files
echo "3. Syntax checking new files..."
for f in \
    src/alerts/fred_signals.py \
    src/alerts/market_breadth.py \
    src/alerts/sentiment.py \
    src/alerts/earnings.py \
    src/learning/regime_tracker.py \
    src/learning/config_patcher.py \
    src/learning/weight_optimizer.py \
    scripts/weekly_recap.py; do
    python3 -c "import ast; ast.parse(open('$f').read())" && echo "   $f OK" || echo "   $f FAIL"
done

# 4. Import check
echo "4. Import checks..."
python3 -c "
import sys; sys.path.insert(0, '.')
from src.alerts.fred_signals import macro_risk_score
from src.alerts.market_breadth import market_breadth_score
from src.alerts.sentiment import compute_sentiment_score
from src.alerts.earnings import earnings_risk_score
from src.learning.regime_tracker import snap_regime, regime_calibration_block
from src.learning.config_patcher import pending_patches
from src.learning.weight_optimizer import optimize_weights
from src.learning.calibration import calibration_block, ticker_calibration_block
print('   All imports OK')
"

# 5. Enable new systemd timer
echo "5. Setting up weekly-recap timer..."
sudo cp scripts/systemd/invest-pi-weekly-recap.service /etc/systemd/system/ 2>/dev/null || true
sudo cp scripts/systemd/invest-pi-weekly-recap.timer /etc/systemd/system/ 2>/dev/null || true
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl enable invest-pi-weekly-recap.timer 2>/dev/null || true
sudo systemctl start invest-pi-weekly-recap.timer 2>/dev/null || true
echo "   Timer enabled"

# 6. Quick smoke test
echo "6. Smoke test..."
python3 -c "
import sys; sys.path.insert(0, '.')
from src.common.storage import LEARNING_DB, TRADING_DB, connect

# Check regime_snapshots table exists
with connect(LEARNING_DB) as conn:
    conn.execute('SELECT count(*) FROM regime_snapshots').fetchone()
    print('   regime_snapshots table OK')

# Check config_patch_log table exists
with connect(LEARNING_DB) as conn:
    conn.execute('SELECT count(*) FROM config_patch_log').fetchone()
    print('   config_patch_log table OK')

# Check calibration block runs
from src.learning.calibration import calibration_block
block = calibration_block('daily_score')
print(f'   calibration_block: {len(block)} chars')

print('   Smoke test PASSED')
"

# 7. Show timer status
echo ""
echo "=== Timer Status ==="
systemctl list-timers --no-pager invest-pi-* 2>/dev/null || echo "(systemctl not available)"

echo ""
echo "=== Deploy complete! ==="
