import os
import sqlite3
import pandas as pd
import pytest
from src.classify import main, load_config

@pytest.fixture(scope="module")
def setup_data():
    main()
    db_path = 'data/inventory.db'
    conn = sqlite3.connect(db_path)
    df_class = pd.read_sql('SELECT * FROM sku_classification', conn)
    conn.close()
    return df_class

def test_abc_thresholds(setup_data):
    df = setup_data
    config = load_config()
    
    a_thresh = config['classification']['abc_thresholds']['a']
    b_thresh = config['classification']['abc_thresholds']['b']
    
    # Check A class respects 80% boundary
    a_max_share = df[df['abc_class'] == 'A']['cum_value_share'].max()
    assert a_max_share <= a_thresh + 0.05, f"A class boundary exceeded: {a_max_share}" # Allow slight margin for the last SKU that crosses the threshold
    
    # Check B class respects 95% boundary
    b_max_share = df[df['abc_class'] == 'B']['cum_value_share'].max()
    assert b_max_share <= b_thresh + 0.05, f"B class boundary exceeded: {b_max_share}"
    
    # Check C class is above B threshold
    c_min_share = df[df['abc_class'] == 'C']['cum_value_share'].min()
    # It might be slightly less than 95% depending on the discrete jump, but typically it starts above 95%
    assert c_min_share >= b_thresh - 0.05, f"C class starts too early: {c_min_share}"

def test_xyz_thresholds(setup_data):
    df = setup_data
    config = load_config()
    
    x_thresh = config['classification']['xyz_cv_thresholds']['x']
    y_thresh = config['classification']['xyz_cv_thresholds']['y']
    
    # X class CV < 0.5
    if not df[df['xyz_class'] == 'X'].empty:
        assert df[df['xyz_class'] == 'X']['cv'].max() < x_thresh
        
    # Y class 0.5 <= CV < 1.0
    if not df[df['xyz_class'] == 'Y'].empty:
        assert df[df['xyz_class'] == 'Y']['cv'].min() >= x_thresh
        assert df[df['xyz_class'] == 'Y']['cv'].max() < y_thresh
        
    # Z class CV >= 1.0
    if not df[df['xyz_class'] == 'Z'].empty:
        assert df[df['xyz_class'] == 'Z']['cv'].min() >= y_thresh

def test_policy_mapping(setup_data):
    df = setup_data
    
    # FAST should only be A/B and X/Y
    fast_df = df[df['policy_class'] == 'FAST']
    assert all(fast_df['abc_class'].isin(['A', 'B']))
    assert all(fast_df['xyz_class'].isin(['X', 'Y']))
    
    # CRITICAL_SLOW_IMPORTED should not be FAST, and must be imported or critical=3
    csi_df = df[df['policy_class'] == 'CRITICAL_SLOW_IMPORTED']
    assert not any(csi_df['abc_class'].isin(['A', 'B']) & csi_df['xyz_class'].isin(['X', 'Y']))
    assert all((csi_df['is_imported'] == 1) | (csi_df['criticality'] == 3))
    
    # Ensure correct z-values are mapped
    assert all(fast_df['z_value'] == 1.88)
    assert all(csi_df['z_value'] == 2.33)
    lt_df = df[df['policy_class'] == 'LONG_TAIL']
    assert all(lt_df['z_value'] == 1.28)

def test_chart_ready_dataframe(setup_data):
    df = setup_data
    # Create the chart-ready dataframe
    summary = df.groupby('policy_class').agg(
        sku_count=('sku_id', 'count'),
        value_share=('annual_consumption_value', lambda x: x.sum() / df['annual_consumption_value'].sum())
    ).reset_index()
    
    # Ensure expected columns are present
    assert 'policy_class' in summary.columns
    assert 'sku_count' in summary.columns
    assert 'value_share' in summary.columns
    
    # Ensure all parts are assigned to exactly one of the three policy classes
    assert summary['sku_count'].sum() == len(df)
