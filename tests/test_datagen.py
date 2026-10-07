import os
import sqlite3
import pandas as pd
import pytest
from src.datagen import main, load_config

@pytest.fixture(scope="module")
def setup_data():
    # Run data generation
    main()
    db_path = 'data/inventory.db'
    conn = sqlite3.connect(db_path)
    
    df_parts = pd.read_sql('SELECT * FROM parts_master', conn)
    df_stock = pd.read_sql('SELECT * FROM opening_stock', conn)
    
    conn.close()
    return df_parts, df_stock

def test_sku_count(setup_data):
    df_parts, _ = setup_data
    config = load_config()
    assert len(df_parts) == config['data']['num_skus'], "SKU count mismatch"

def test_inventory_value(setup_data):
    _, df_stock = setup_data
    total_value = df_stock['value'].sum()
    total_value_cr = total_value / 10_000_000
    assert 5.5 <= total_value_cr <= 6.5, f"Inventory value {total_value_cr} Cr not in range [5.5, 6.5]"

def test_pareto_share(setup_data):
    _, df_stock = setup_data
    df_pareto = df_stock.sort_values(by='value', ascending=False)
    top_20_percent = int(0.2 * len(df_pareto))
    top_20_val = df_pareto.head(top_20_percent)['value'].sum()
    total_val = df_stock['value'].sum()
    share = top_20_val / total_val
    assert share >= 0.70, f"Pareto share {share*100:.1f}% is less than 70%"

def test_imported_share(setup_data):
    df_parts, _ = setup_data
    imported_ratio = df_parts['is_imported'].mean()
    assert 0.55 <= imported_ratio <= 0.65, f"Imported share {imported_ratio*100:.1f}% is not about 60%"

def test_reproducibility():
    # Run it twice and check if they are identical
    db_path = 'data/inventory.db'
    
    main()
    conn1 = sqlite3.connect(db_path)
    df_stock1 = pd.read_sql('SELECT * FROM opening_stock', conn1)
    conn1.close()
    
    main()
    conn2 = sqlite3.connect(db_path)
    df_stock2 = pd.read_sql('SELECT * FROM opening_stock', conn2)
    conn2.close()
    
    pd.testing.assert_frame_equal(df_stock1, df_stock2)
