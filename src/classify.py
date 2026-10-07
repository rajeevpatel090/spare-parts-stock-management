import os
import yaml
import sqlite3
import pandas as pd
import numpy as np

def load_config(config_path="config.yaml"):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def compute_classification(df_parts, df_stock, df_consumption, config):
    days = config['simulation']['days']
    
    # Ensure all SKUs have all days accounted for to calculate proper mean and std
    # We can calculate this more efficiently mathematically
    
    # 1. Calculate sum, mean, std per SKU
    # First, sum of squares and sum
    if not df_consumption.empty:
        agg = df_consumption.groupby('sku_id')['quantity'].agg(['sum', 'count', lambda x: (x**2).sum()])
        agg.columns = ['sum_qty', 'days_with_demand', 'sum_sq_qty']
    else:
        agg = pd.DataFrame(columns=['sum_qty', 'days_with_demand', 'sum_sq_qty'], index=pd.Index([], name='sku_id'))
        
    df = df_parts[['sku_id', 'is_imported', 'criticality', 'unit_cost']].copy()
    df = df.merge(agg, on='sku_id', how='left').fillna(0)
    
    # Mean daily demand
    df['mean_daily_qty'] = df['sum_qty'] / days
    
    # Std daily demand
    # Variance = (sum(x^2) / N) - (mean)^2
    variance = (df['sum_sq_qty'] / days) - (df['mean_daily_qty'] ** 2)
    # Handle floating point inaccuracies leading to negative very small variance
    variance = variance.clip(lower=0)
    df['std_daily_qty'] = np.sqrt(variance)
    
    # Annual consumption value
    df['annual_consumption_value'] = df['mean_daily_qty'] * 365 * df['unit_cost']
    
    # ABC Classification
    df = df.sort_values(by='annual_consumption_value', ascending=False).reset_index(drop=True)
    total_value = df['annual_consumption_value'].sum()
    
    if total_value > 0:
        df['cum_value_share'] = df['annual_consumption_value'].cumsum() / total_value
    else:
        df['cum_value_share'] = 1.0
        
    a_thresh = config['classification']['abc_thresholds']['a']
    b_thresh = config['classification']['abc_thresholds']['b']
    
    df['abc_class'] = 'C'
    df.loc[df['cum_value_share'] <= b_thresh, 'abc_class'] = 'B'
    df.loc[df['cum_value_share'] <= a_thresh, 'abc_class'] = 'A'
    
    # XYZ Classification
    # CV = std / mean
    df['cv'] = np.where(df['mean_daily_qty'] > 0, df['std_daily_qty'] / df['mean_daily_qty'], np.inf)
    
    x_thresh = config['classification']['xyz_cv_thresholds']['x']
    y_thresh = config['classification']['xyz_cv_thresholds']['y']
    
    df['xyz_class'] = 'Z'
    df.loc[df['cv'] < y_thresh, 'xyz_class'] = 'Y'
    df.loc[df['cv'] < x_thresh, 'xyz_class'] = 'X'
    
    # Policy Class
    df['policy_class'] = 'LONG_TAIL'
    
    # FAST: A or B, and X or Y
    fast_mask = df['abc_class'].isin(['A', 'B']) & df['xyz_class'].isin(['X', 'Y'])
    df.loc[fast_mask, 'policy_class'] = 'FAST'
    
    # CRITICAL_SLOW_IMPORTED: not FAST, and (criticality == 3 or is_imported == 1)
    csi_mask = (~fast_mask) & ((df['criticality'] == 3) | (df['is_imported'] == 1))
    df.loc[csi_mask, 'policy_class'] = 'CRITICAL_SLOW_IMPORTED'
    
    # Map Service Levels
    sl_config = config['classification']['service_levels']
    
    df['service_level'] = df['policy_class'].map(lambda x: sl_config[x]['target'])
    df['z_value'] = df['policy_class'].map(lambda x: sl_config[x]['z_value'])
    
    return df

def main():
    print("Running classification engine...")
    config = load_config()
    
    db_path = 'data/inventory.db'
    conn = sqlite3.connect(db_path)
    
    df_parts = pd.read_sql('SELECT * FROM parts_master', conn)
    df_stock = pd.read_sql('SELECT * FROM opening_stock', conn)
    df_consumption = pd.read_sql('SELECT * FROM consumption', conn)
    
    df_class = compute_classification(df_parts, df_stock, df_consumption, config)
    
    # Save back to DB
    df_class.to_sql('sku_classification', conn, index=False, if_exists='replace')
    conn.close()
    
    print("Classification complete. Saved to sku_classification table.")
    
    # Print summary
    summary = df_class.groupby('policy_class').agg(
        sku_count=('sku_id', 'count'),
        value_share=('annual_consumption_value', lambda x: x.sum() / df_class['annual_consumption_value'].sum())
    ).reset_index()
    
    print("\nPolicy Class Summary:")
    for _, row in summary.iterrows():
        print(f"{row['policy_class']:<25}: {row['sku_count']} SKUs, {row['value_share']*100:.1f}% value share")

if __name__ == "__main__":
    main()
