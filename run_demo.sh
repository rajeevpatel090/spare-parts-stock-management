#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " Spare Parts Stock Intelligence - Full End-to-End Demo"
echo "=========================================================="

echo "[1/5] Generating synthetic master data & demand history..."
python src/datagen.py

echo "[2/5] Running SKU classification engine (ABC-XYZ-Policy)..."
python src/classify.py

echo "[3/5] Computing dynamic inventory stocking policies..."
python src/policy.py

echo "[4/5] Running calibration & 12-month Monte Carlo simulation..."
python src/simulate.py

echo "[5/5] Launching Streamlit dashboard..."
streamlit run app/main.py
