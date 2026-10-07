import numpy as np
import pandas as pd
import sqlite3
import yaml
import json
import os

def load_config(config_path="config.yaml"):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def save_config(config, config_path="config.yaml"):
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

def build_scenario_demand(config, df_parts, seed, days=365):
    """
    Generates a deterministic but stochastic daily demand matrix (days x SKUs)
    using Poisson distribution around the historical mean.
    """
    np_random = np.random.RandomState(seed)
    num_skus = len(df_parts)
    means = df_parts['mean_daily_qty'].values
    
    demand = np_random.poisson(lam=means, size=(days, num_skus))
    return demand

def run_simulation(config, df_parts, demand_matrix, scenario='baseline'):
    """
    Vectorized day-by-day simulation of 1 year for one centre.
    
    The phantom-stock loop (core insight):
      In LEGACY mode, a share of issues go unrecorded in the book.
      Book stock stays high -> reorder never fires -> physical hits 0 -> emergency order.
      In GATED mode, every issue is recorded, so book tracks physical and reorders
      fire on time.
    """
    days = demand_matrix.shape[0]
    num_skus = len(df_parts)
    
    # Extract SKU arrays
    unit_cost = df_parts['unit_cost'].values
    lead_time = df_parts['lead_time_days'].values.astype(int)
    abc_class = df_parts['abc_class'].values
    policy_class = df_parts['policy_class'].values
    is_imported = df_parts['is_imported'].values
    mean_daily = df_parts['mean_daily_qty'].values
    
    # For daily/weekly counts (deterministic risk scoring)
    if 'theft_risk' in df_parts.columns:
        theft_risk = df_parts['theft_risk'].values
    else:
        theft_risk = np.random.RandomState(77).uniform(0.1, 1.0, num_skus)
    value_risk = unit_cost * mean_daily * 365 * theft_risk
    top_50_idx = np.argsort(value_risk)[-50:]
    a_class_idx = np.where(abc_class == 'A')[0]
    
    # Scenario switches
    use_m3_rop = scenario in ['stocking', 'proposed']
    use_gated = scenario in ['tracing', 'proposed']
    use_pooling = scenario in ['stocking', 'proposed']
    
    untracked_share = 0.0 if use_gated else config['ledger'].get('legacy_untracked_share', 0.026)
    
    # --- Initial state ---
    baseline_stock_factor = config['baseline'].get('stock_factor', 0.15)
    if scenario in ['baseline', 'tracing']:
        opening = np.floor(df_parts['opening_qty'].values * baseline_stock_factor).astype(float)
    else:
        opening = df_parts['opening_qty'].values.copy().astype(float)
    
    book_qty = opening.copy()
    physical_qty = opening.copy()
    on_order = np.zeros(num_skus)
    order_arrivals = np.zeros((days + 60, num_skus))  # buffer for late arrivals
    
    # --- Reorder policies ---
    if use_m3_rop:
        rop = df_parts['rop'].values.astype(float)
        max_level = df_parts['max_level'].values.astype(float)
    else:
        # Baseline: static min-max from last year's average (ignores lead-time variability and criticality)
        static_days = config['baseline'].get('static_coverage_days', 0.6)
        rop = np.ceil(mean_daily * static_days)
        max_level = rop + np.maximum(1, np.ceil(mean_daily * config['policy'].get('review_period_days', 14)))
    
    # Sister pooling capacity (only for proposed/stocking)
    if use_pooling:
        sister_stock = np.where(
            policy_class == 'CRITICAL_SLOW_IMPORTED',
            np.random.RandomState(55).randint(0, 3, num_skus), 0
        ).astype(float)
    else:
        sister_stock = np.zeros(num_skus)
        
    # --- Accumulators ---
    total_demand = 0
    total_emergency_orders = 0
    total_emergency_premium = 0.0
    total_shrinkage_val = 0.0
    car_waiting_days = 0.0
    first_visit_fill = 0
    total_visits = 0
    daily_inventory_value = 0.0
    
    np_random = np.random.RandomState(42)
    premium_min = config['baseline'].get('emergency_premium_min', 0.35)
    premium_max = config['baseline'].get('emergency_premium_max', 0.50)
    
    for day in range(days):
        # 1. Receive arrivals
        arrived = order_arrivals[day]
        book_qty += arrived
        physical_qty += arrived
        on_order = np.maximum(0, on_order - arrived)  # clip to 0
        
        # 2. Demand & Consumption
        D = demand_matrix[day].astype(float)
        total_demand += np.sum(D)
        
        consumed = np.minimum(physical_qty, D)
        shortfall = D - consumed   # units that couldn't be served from shelf
        
        # 3. Handle shortfalls
        # Sister pooling: try inter-branch transfer before emergency order
        if use_pooling:
            can_pool = (shortfall > 0) & (sister_stock > 0) & (policy_class == 'CRITICAL_SLOW_IMPORTED')
            pooled = np.minimum(shortfall, sister_stock) * can_pool
            sister_stock -= pooled
            shortfall -= pooled
            # Pooled units arrive next day (1-day transfer)
            if day + 1 < days + 60:
                order_arrivals[day + 1] += pooled
                on_order += pooled
            
        # Remaining shortfalls -> emergency OEM orders
        num_shortfall = np.sum(shortfall)
        total_emergency_orders += num_shortfall
        if num_shortfall > 0:
            premium_rate = np_random.uniform(premium_min, premium_max, num_skus)
            total_emergency_premium += np.sum(shortfall * unit_cost * premium_rate)
            # Car-waiting days (imported parts wait longer)
            car_waiting_days += np.sum(shortfall * np.where(is_imported == 1, 7, 3))
        
        # Fill rate on first visit
        total_visits += np.sum(D > 0)
        first_visit_fill += np.sum((D > 0) & (shortfall == 0))
        
        # 4. Ledger updates — the phantom-stock mechanism
        physical_qty -= consumed
        
        # In LEGACY mode, a random share of issues are untracked (verbal handouts).
        # Book is NOT decremented for these -> phantom stock builds up.
        tracked_mask = np_random.random(num_skus) > untracked_share
        book_consumed = consumed * tracked_mask
        book_qty -= book_consumed
        
        # Shrinkage = value of units consumed but not booked
        untracked_val = np.sum((consumed - book_consumed) * unit_cost)
        total_shrinkage_val += untracked_val
        
        # 5. Scheduled counts (GATED / tracing mode only)
        #    These correct the phantom stock by syncing book to physical.
        if use_gated:
            # Daily count on top 50 high-risk parts
            book_qty[top_50_idx] = physical_qty[top_50_idx]
            # Weekly cycle count on A-class parts
            if day % 7 == 0:
                book_qty[a_class_idx] = physical_qty[a_class_idx]
                
        # 6. Reorder logic — triggered off BOOK stock (the key failure mode)
        #    In LEGACY mode, book is inflated -> reorder doesn't fire -> stock-out.
        effective_position = book_qty + on_order
        needs_order = effective_position <= rop
        valid_order = needs_order & (max_level > effective_position)
        
        order_qty = np.maximum(0, max_level - effective_position) * valid_order
        
        on_order += order_qty
        # Schedule arrivals based on lead time using fast np.add.at
        ord_idx = np.where(order_qty > 0)[0]
        if len(ord_idx) > 0:
            arrival_days = np.minimum(day + lead_time, days + 59)
            np.add.at(order_arrivals, (arrival_days[ord_idx], ord_idx), order_qty[ord_idx])
            
        # 7. Track daily inventory value
        daily_inventory_value += np.sum(physical_qty * unit_cost)
        
    # --- Summarize ---
    avg_inventory_value = daily_inventory_value / days
    emergency_rate = total_emergency_orders / total_demand if total_demand > 0 else 0
    shrinkage_pct = total_shrinkage_val / avg_inventory_value if avg_inventory_value > 0 else 0
    fill_rate = first_visit_fill / total_visits if total_visits > 0 else 0
    
    return {
        'emergency_rate': float(emergency_rate),
        'emergency_premium_inr': float(total_emergency_premium),
        'shrinkage_val_inr': float(total_shrinkage_val),
        'shrinkage_pct': float(shrinkage_pct),
        'car_waiting_days': float(car_waiting_days),
        'fill_rate': float(fill_rate),
        'avg_inventory_value_inr': float(avg_inventory_value)
    }

def calibrate_baseline(config, df_parts):
    """
    Jointly tunes baseline parameters so that baseline simulation
    lands at emergency_rate ~ 22% (+/- 2%) and shrinkage ~ 3% (+/- 0.5%) of inventory value.
    """
    print("Calibrating baseline parameters...")
    target_er = config['baseline'].get('emergency_rate', 0.22)    # 0.22
    target_sh = config['baseline'].get('shrinkage_rate', 0.03)    # 0.03
    
    config['baseline']['static_coverage_days'] = 0.6
    
    cal_demands = [build_scenario_demand(config, df_parts, seed=s) for s in [101, 102, 103]]
    
    def eval_params(sf, us):
        config['baseline']['stock_factor'] = sf
        config['ledger']['legacy_untracked_share'] = us
        ers = []
        shs = []
        for d in cal_demands:
            r = run_simulation(config, df_parts, d, 'baseline')
            ers.append(r['emergency_rate'])
            shs.append(r['shrinkage_pct'])
        return np.mean(ers), np.mean(shs)
    
    # 1. Calibrate stock_factor for emergency rate
    low_sf, high_sf = 0.08, 0.35
    best_sf = 0.15
    current_us = 0.0062
    for _ in range(8):
        mid_sf = (low_sf + high_sf) / 2
        er, _ = eval_params(mid_sf, current_us)
        if er > target_er:
            low_sf = mid_sf
        else:
            high_sf = mid_sf
        best_sf = mid_sf
        if abs(er - target_er) < 0.005:
            break
            
    config['baseline']['stock_factor'] = float(best_sf)
    
    # 2. Calibrate untracked_share for shrinkage
    low_us, high_us = 0.003, 0.015
    best_us = 0.0062
    for _ in range(8):
        mid_us = (low_us + high_us) / 2
        _, sh = eval_params(config['baseline']['stock_factor'], mid_us)
        if sh < target_sh:
            low_us = mid_us
        else:
            high_us = mid_us
        best_us = mid_us
        if abs(sh - target_sh) < 0.002:
            break
            
    config['ledger']['legacy_untracked_share'] = float(best_us)
    
    # Final check
    er_final, sh_final = eval_params(config['baseline']['stock_factor'], config['ledger']['legacy_untracked_share'])
    save_config(config)
    print(f"Calibrated parameters:")
    print(f"  stock_factor       = {config['baseline']['stock_factor']:.4f}")
    print(f"  untracked_share    = {config['ledger']['legacy_untracked_share']:.4f}")
    print(f"  >> Emergency rate  = {er_final*100:.2f}%  (target {target_er*100:.0f}%)")
    print(f"  >> Shrinkage       = {sh_final*100:.2f}% (target {target_sh*100:.0f}%)")
    return config

def run_monte_carlo(config, df_parts, num_seeds=20):
    print(f"Running Monte Carlo simulation ({num_seeds} seeds x 4 scenarios)...")
    scenarios = ['baseline', 'stocking', 'tracing', 'proposed']
    results = {s: [] for s in scenarios}
    
    for seed in range(num_seeds):
        demand = build_scenario_demand(config, df_parts, seed)
        for s in scenarios:
            res = run_simulation(config, df_parts, demand, s)
            results[s].append(res)
            
    # Aggregate
    agg_results = {}
    for s in scenarios:
        df_res = pd.DataFrame(results[s])
        agg_results[s] = {
            'emergency_rate': {
                'mean': float(df_res['emergency_rate'].mean()),
                'p10': float(df_res['emergency_rate'].quantile(0.1)),
                'p90': float(df_res['emergency_rate'].quantile(0.9))
            },
            'shrinkage_pct': {
                'mean': float(df_res['shrinkage_pct'].mean()),
                'p10': float(df_res['shrinkage_pct'].quantile(0.1)),
                'p90': float(df_res['shrinkage_pct'].quantile(0.9))
            },
            'fill_rate': {
                'mean': float(df_res['fill_rate'].mean()),
                'p10': float(df_res['fill_rate'].quantile(0.1)),
                'p90': float(df_res['fill_rate'].quantile(0.9))
            },
            'car_waiting_days': float(df_res['car_waiting_days'].mean()),
            'emergency_premium_inr': float(df_res['emergency_premium_inr'].mean()),
            'shrinkage_val_inr': float(df_res['shrinkage_val_inr'].mean()),
            'avg_inventory_value_inr': float(df_res['avg_inventory_value_inr'].mean())
        }
        
    return agg_results

def ensure_classification_and_policy(conn, config):
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sku_classification'")
    has_class = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sku_policy'")
    has_policy = cursor.fetchone() is not None
    
    if not has_class:
        import src.classify
        print("sku_classification missing, generating...")
        src.classify.main()
        
    if not has_policy:
        import src.policy
        print("sku_policy missing, generating...")
        src.policy.main()

def main():
    config = load_config()
    db_path = 'data/inventory.db'
    conn = sqlite3.connect(db_path)
    ensure_classification_and_policy(conn, config)
    
    df_parts = pd.read_sql(
        'SELECT p.*, s.opening_qty, s.value, c.abc_class, c.policy_class, '
        'c.mean_daily_qty, c.std_daily_qty, pol.rop, pol.max_level, pol.safety_stock '
        'FROM parts_master p '
        'JOIN opening_stock s ON p.sku_id = s.sku_id '
        'JOIN sku_classification c ON p.sku_id = c.sku_id '
        'JOIN sku_policy pol ON p.sku_id = pol.sku_id', conn)
    conn.close()
    
    # Calibration
    config = calibrate_baseline(config, df_parts)
    
    # Monte Carlo simulation (20 seeds for speed while keeping high statistical precision)
    results = run_monte_carlo(config, df_parts, num_seeds=20)
    
    # Print results table
    print("\n" + "=" * 95)
    print("SIMULATION RESULTS  (Mean [p10 - p90] over Monte Carlo seeds)")
    print("=" * 95)
    header = f"{'Scenario':<12} | {'Emerg %':<18} | {'Shrinkage %':<18} | {'Fill Rate %':<18} | {'Prem (Lakh)':<12} | {'Shrink (Lakh)':<12}"
    print(header)
    print("-" * 95)
    for s in ['baseline', 'stocking', 'tracing', 'proposed']:
        r = results[s]
        er = r['emergency_rate']
        sh = r['shrinkage_pct']
        fr = r['fill_rate']
        prem = r['emergency_premium_inr'] / 100_000  # to Lakhs
        shr_val = r['shrinkage_val_inr'] / 100_000
        
        er_str = f"{er['mean']*100:5.1f} [{er['p10']*100:.1f}-{er['p90']*100:.1f}]"
        sh_str = f"{sh['mean']*100:5.2f} [{sh['p10']*100:.2f}-{sh['p90']*100:.2f}]"
        fr_str = f"{fr['mean']*100:5.1f} [{fr['p10']*100:.1f}-{fr['p90']*100:.1f}]"
        
        print(f"{s:<12} | {er_str:<18} | {sh_str:<18} | {fr_str:<18} | {prem:>10.1f}  | {shr_val:>10.1f}")
    print("=" * 95)
    
    with open('data/sim_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to data/sim_results.json")

if __name__ == "__main__":
    main()
