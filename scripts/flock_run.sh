#!/usr/bin/env bash
# flock-Wrapper: verhindert, dass zwei DB-schreibende Jobs gleichzeitig laufen.
# Usage: flock_run.sh <lockname> <command...>
#   flock_run.sh trading python3 -B scripts/run_strategy.py
#   flock_run.sh scoring python3 -B scripts/score_portfolio.py --full-scan
#
# Lock-Gruppen:
#   trading — run_strategy, rebalance, sync_positions, monthly_dca, weekly_rotation
#   scoring — score_portfolio (laeuft alle 30s, darf nicht mit trading kollidieren)
#   learning — train_regime, build_patterns, meta_review, track_outcomes

LOCK_DIR="/home/investpi/invest-pi/data/.locks"
mkdir -p "$LOCK_DIR"

LOCK_NAME="${1:?usage: flock_run.sh <lockname> <command...>}"
shift

LOCK_FILE="$LOCK_DIR/${LOCK_NAME}.lock"

exec flock --nonblock --conflict-exit-code 75 "$LOCK_FILE" "$@"
