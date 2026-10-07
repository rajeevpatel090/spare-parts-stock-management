import os
import json
import pytest
import sqlite3
import numpy as np
import pandas as pd
from src.simulate import main, load_config, run_simulation, build_scenario_demand

@pytest.fixture(scope="module")
def setup_data():
    # Run full pipeline: calibrate + simulate
    main()
    
    config = load_config()
    with open('data/sim_results.json', 'r') as f:
        results = json.load(f)
        
    return config, results

def test_calibration_emergency_rate(setup_data):
    """Baseline emergency rate should be 22% +/- 2%."""
    _, results = setup_data
    er = results['baseline']['emergency_rate']['mean']
    assert 0.20 <= er <= 0.24, f"Baseline emergency rate {er*100:.1f}% outside [20%, 24%]"

def test_calibration_shrinkage(setup_data):
    """Baseline shrinkage should be 3% +/- 0.5%."""
    _, results = setup_data
    sh = results['baseline']['shrinkage_pct']['mean']
    assert 0.025 <= sh <= 0.035, f"Baseline shrinkage {sh*100:.2f}% outside [2.5%, 3.5%]"

def test_results_structure(setup_data):
    _, results = setup_data
    for s in ['baseline', 'stocking', 'tracing', 'proposed']:
        assert s in results
        assert 'emergency_rate' in results[s]
        assert 'mean' in results[s]['emergency_rate']
        assert 'p10' in results[s]['emergency_rate']
        assert 'p90' in results[s]['emergency_rate']
        assert 'shrinkage_pct' in results[s]
        assert 'fill_rate' in results[s]
        assert 'car_waiting_days' in results[s]

def test_proposed_improves_on_baseline(setup_data):
    """The proposed scenario should be strictly better than baseline."""
    _, results = setup_data
    assert results['proposed']['emergency_rate']['mean'] < results['baseline']['emergency_rate']['mean']
    assert results['proposed']['shrinkage_pct']['mean'] < results['baseline']['shrinkage_pct']['mean']

def test_reproducibility():
    config = load_config()
    conn = sqlite3.connect('data/inventory.db')
    df_parts = pd.read_sql(
        'SELECT p.*, s.opening_qty, s.value, c.abc_class, c.policy_class, '
        'c.mean_daily_qty, c.std_daily_qty, pol.rop, pol.max_level, pol.safety_stock '
        'FROM parts_master p '
        'JOIN opening_stock s ON p.sku_id = s.sku_id '
        'JOIN sku_classification c ON p.sku_id = c.sku_id '
        'JOIN sku_policy pol ON p.sku_id = pol.sku_id', conn)
    conn.close()
    
    demand1 = build_scenario_demand(config, df_parts, seed=42)
    demand2 = build_scenario_demand(config, df_parts, seed=42)
    assert np.array_equal(demand1, demand2)
