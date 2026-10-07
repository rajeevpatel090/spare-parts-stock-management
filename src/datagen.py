import os
import yaml
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

def load_config(config_path="config.yaml"):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def generate_parts_master(config, np_random):
    num_skus = config['data']['num_skus']
    imported_share = config['data']['imported_share']
    
    categories = ['brakes', 'filters', 'engine', 'electrical', 'suspension', 'body', 'EV/high-voltage', 'fluids']
    models = config['models']
    
    # Generate SKU IDs
    sku_ids = [f"SKU{str(i).zfill(5)}" for i in range(1, num_skus + 1)]
    
    # Generate categories
    sku_categories = np_random.choice(categories, size=num_skus)
    
    # Generate unit_cost to follow Pareto principle (lognormal is good for this)
    # A lognormal distribution with a suitably large sigma creates a heavy tail
    # We will scale this later so the total opening stock value hits ~6 Cr
    sigma = 1.5
    mean = 5.0
    unit_costs = np_random.lognormal(mean, sigma, size=num_skus)
    
    # Generate is_imported
    is_imported = np_random.choice([1, 0], size=num_skus, p=[imported_share, 1 - imported_share])
    
    # Generate lead times based on import status
    lead_times = []
    for imp in is_imported:
        if imp:
            lt = np_random.randint(config['data']['lead_time']['imported']['min'], 
                                   config['data']['lead_time']['imported']['max'] + 1)
        else:
            lt = np_random.randint(config['data']['lead_time']['local']['min'], 
                                   config['data']['lead_time']['local']['max'] + 1)
        lead_times.append(lt)
        
    # Generate criticality (1, 2, 3)
    # Let's say 1 (can wait) = 50%, 2 (comfort) = 30%, 3 (VOR) = 20%
    criticality = np_random.choice([1, 2, 3], size=num_skus, p=[0.5, 0.3, 0.2])
    
    # Compatible models - string representation of list
    compatible_models = []
    for _ in range(num_skus):
        # A part can be compatible with 1 to 3 models
        num_models = np_random.randint(1, 4)
        comp_mods = np_random.choice(models, size=num_models, replace=False)
        compatible_models.append(",".join(comp_mods))
        
    df_parts = pd.DataFrame({
        'sku_id': sku_ids,
        'name': [f"Part {i}" for i in range(1, num_skus + 1)],
        'category': sku_categories,
        'unit_cost': unit_costs,
        'is_imported': is_imported,
        'lead_time_days': lead_times,
        'criticality': criticality,
        'compatible_models': compatible_models
    })
    
    return df_parts

def generate_bookings(config, np_random):
    days = config['simulation']['days']
    min_bookings = config['simulation']['service_bookings_per_day']['min']
    max_bookings = config['simulation']['service_bookings_per_day']['max']
    models = config['models']
    
    # 12 months history
    start_date = datetime(2023, 1, 1)
    dates = [start_date + timedelta(days=i) for i in range(days)]
    
    records = []
    
    for d in dates:
        # Weekday seasonality (fewer on weekends)
        is_weekend = d.weekday() >= 5
        base_cars = np_random.randint(min_bookings, max_bookings + 1)
        if is_weekend:
            base_cars = max(10, int(base_cars * 0.5))
            
        # Monsoon bump (June - Sept)
        if d.month in [6, 7, 8, 9]:
            base_cars = int(base_cars * 1.2)
            
        # Festive bump (Oct - Nov)
        if d.month in [10, 11]:
            base_cars = int(base_cars * 1.3)
            
        # Service types: minor, major, repair, accident
        service_types = ['minor', 'major', 'repair', 'accident']
        service_probs = [0.5, 0.3, 0.15, 0.05]
        
        for _ in range(base_cars):
            model = np_random.choice(models)
            service_type = np_random.choice(service_types, p=service_probs)
            records.append({
                'date': d,
                'model': model,
                'service_type': service_type
            })
            
    return pd.DataFrame(records)

def generate_bom_and_consumption(df_parts, df_bookings, np_random):
    """
    Creates a simulated BOM and daily consumption history based on bookings.
    """
    # Create BOM logically
    # A minor service uses filters, fluids
    # A major service uses filters, fluids, spark plugs (engine), brakes
    # Repair uses engine, electrical, suspension
    # Accident uses body, electrical, suspension
    
    sku_cats = df_parts.set_index('sku_id')['category'].to_dict()
    sku_models = {row['sku_id']: row['compatible_models'].split(',') for _, row in df_parts.iterrows()}
    
    # We don't need an explicit huge BOM table if we generate consumption directly from bookings
    # But let's build a probability map for each category per service type
    service_cat_probs = {
        'minor': {'filters': 0.8, 'fluids': 0.9, 'brakes': 0.05, 'electrical': 0.01, 'suspension': 0.0, 'engine': 0.0, 'body': 0.0, 'EV/high-voltage': 0.01},
        'major': {'filters': 0.95, 'fluids': 0.95, 'brakes': 0.4, 'electrical': 0.05, 'suspension': 0.1, 'engine': 0.1, 'body': 0.01, 'EV/high-voltage': 0.05},
        'repair': {'filters': 0.1, 'fluids': 0.2, 'brakes': 0.2, 'electrical': 0.4, 'suspension': 0.3, 'engine': 0.4, 'body': 0.05, 'EV/high-voltage': 0.1},
        'accident': {'filters': 0.05, 'fluids': 0.2, 'brakes': 0.1, 'electrical': 0.3, 'suspension': 0.5, 'engine': 0.2, 'body': 0.9, 'EV/high-voltage': 0.1}
    }
    
    # Pre-group SKUs by (category, model) to speed up sampling
    from collections import defaultdict
    cat_model_skus = defaultdict(list)
    for sku, cat in sku_cats.items():
        for mod in sku_models[sku]:
            cat_model_skus[(cat, mod)].append(sku)
            
    consumption_records = []
    
    for _, row in df_bookings.iterrows():
        d = row['date']
        mod = row['model']
        stype = row['service_type']
        probs = service_cat_probs[stype]
        
        for cat, prob in probs.items():
            if np_random.random() < prob:
                # pick a random SKU from this cat and model
                available_skus = cat_model_skus.get((cat, mod), [])
                if available_skus:
                    # pick 1 or 2 items
                    n_items = np_random.randint(1, 3)
                    chosen = np_random.choice(available_skus, size=n_items, replace=True)
                    for sku in chosen:
                        qty = np_random.randint(1, 4)
                        consumption_records.append({
                            'date': d,
                            'sku_id': sku,
                            'quantity': qty
                        })
                        
    df_consumption = pd.DataFrame(consumption_records)
    if not df_consumption.empty:
        df_consumption = df_consumption.groupby(['date', 'sku_id'])['quantity'].sum().reset_index()
    else:
        df_consumption = pd.DataFrame(columns=['date', 'sku_id', 'quantity'])
        
    return df_consumption

def compute_opening_stock(df_parts, df_consumption, config, np_random):
    # To hit ~6 Cr, and ensure 20% SKUs hold ~80% value, we rely on the lognormal unit_cost
    # and we generate an opening stock quantity that reflects demand.
    
    total_target_value = config['data']['target_inventory_value_cr'] * 10_000_000 # in Rs
    
    # Calculate avg daily demand for each SKU
    if not df_consumption.empty:
        total_demand = df_consumption.groupby('sku_id')['quantity'].sum()
        days = config['simulation']['days']
        avg_daily = total_demand / days
    else:
        avg_daily = pd.Series(0, index=df_parts['sku_id'])
        
    df_stock = df_parts[['sku_id', 'unit_cost', 'lead_time_days']].copy()
    df_stock = df_stock.merge(avg_daily.rename('avg_daily'), on='sku_id', how='left').fillna({'avg_daily': 0})
    
    # Give everyone at least some base stock based on lead time + safety factor
    # Slow movers get random small amounts
    base_qty = np.ceil(df_stock['avg_daily'] * df_stock['lead_time_days'] * 1.5)
    noise_qty = np_random.randint(0, 5, size=len(df_stock))
    df_stock['opening_qty'] = base_qty + noise_qty
    
    # Calculate current value
    df_stock['initial_value'] = df_stock['opening_qty'] * df_stock['unit_cost']
    current_total_value = df_stock['initial_value'].sum()
    
    # Scale unit costs to exactly hit the target value
    # This ensures unit_cost * opening_qty sums to exactly 6 Cr
    scaling_factor = total_target_value / current_total_value
    df_parts['unit_cost'] = df_parts['unit_cost'] * scaling_factor
    
    # Recalculate value with scaled cost
    df_stock['unit_cost'] = df_stock['unit_cost'] * scaling_factor
    df_stock['value'] = df_stock['opening_qty'] * df_stock['unit_cost']
    
    df_stock = df_stock[['sku_id', 'opening_qty', 'value']]
    df_stock['book_qty'] = df_stock['opening_qty']
    df_stock['physical_qty'] = df_stock['opening_qty']
    
    return df_parts, df_stock

def main():
    print("Generating synthetic data...")
    config = load_config()
    seed = config['simulation'].get('seed', 42)
    np_random = np.random.RandomState(seed)
    
    # 1. Parts Master
    df_parts = generate_parts_master(config, np_random)
    
    # 2. Bookings
    df_bookings = generate_bookings(config, np_random)
    
    # 3 & 4. Consumption
    df_consumption = generate_bom_and_consumption(df_parts, df_bookings, np_random)
    
    # 5. Opening Stock
    df_parts, df_stock = compute_opening_stock(df_parts, df_consumption, config, np_random)
    
    # Save to SQLite
    os.makedirs('data', exist_ok=True)
    db_path = 'data/inventory.db'
    
    # If DB exists, remove it for a fresh run
    if os.path.exists(db_path):
        os.remove(db_path)
        
    conn = sqlite3.connect(db_path)
    df_parts.to_sql('parts_master', conn, index=False, if_exists='replace')
    df_bookings.to_sql('bookings', conn, index=False, if_exists='replace')
    df_consumption.to_sql('consumption', conn, index=False, if_exists='replace')
    df_stock.to_sql('opening_stock', conn, index=False, if_exists='replace')
    conn.close()
    
    print("Data generation complete. Saved to data/inventory.db")
    
    # Print summary
    total_val = df_stock['value'].sum()
    print(f"Total Opening Value: INR {total_val / 10_000_000:.2f} Cr")
    
    # Check Pareto
    df_pareto = df_stock.sort_values(by='value', ascending=False)
    top_20_percent = int(0.2 * len(df_pareto))
    top_20_val = df_pareto.head(top_20_percent)['value'].sum()
    print(f"Top 20% SKUs value share: {(top_20_val / total_val) * 100:.1f}%")
    
    imported_ratio = df_parts['is_imported'].mean()
    print(f"Imported share: {imported_ratio * 100:.1f}%")

if __name__ == "__main__":
    main()
