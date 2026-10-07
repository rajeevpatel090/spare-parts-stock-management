import numpy as np
import pandas as pd
import pytest
from src.policy import compute_policy_params, InventoryEngine

def test_worked_example():
    """
    Worked check: demand 1.2/day, LT 12 days, sigma 0.8 gives SS about 6.5 (round to 7) and ROP about 22.
    """
    df_class = pd.DataFrame([{
        'sku_id': 'TEST001',
        'mean_daily_qty': 1.2,
        'std_daily_qty': 0.8,
        'z_value': 2.33 # typical for critical
    }])
    df_parts = pd.DataFrame([{
        'sku_id': 'TEST001',
        'lead_time_days': 12
    }])
    
    df_policy = compute_policy_params(df_class, df_parts, review_period_days=14)
    
    # SS = ceil(2.33 * 0.8 * sqrt(12)) = ceil(6.457) = 7.0
    assert df_policy.loc[0, 'safety_stock'] == 7.0
    
    # ROP = ceil(1.2 * 12 + 7) = ceil(14.4 + 7) = ceil(21.4) = 22.0
    assert df_policy.loc[0, 'rop'] == 22.0

def test_rop_never_below_ss():
    # Test with random variations to ensure ROP >= SS
    df_class = pd.DataFrame({
        'sku_id': [f'SKU{i}' for i in range(100)],
        'mean_daily_qty': np.random.uniform(0.1, 5.0, 100),
        'std_daily_qty': np.random.uniform(0.1, 3.0, 100),
        'z_value': np.random.choice([1.28, 1.88, 2.33], 100)
    })
    df_parts = pd.DataFrame({
        'sku_id': [f'SKU{i}' for i in range(100)],
        'lead_time_days': np.random.randint(1, 45, 100)
    })
    
    df_policy = compute_policy_params(df_class, df_parts)
    assert (df_policy['rop'] >= df_policy['safety_stock']).all()

def test_pooling_reduces_emergencies():
    # Small deterministic test
    df_policy = pd.DataFrame([{
        'sku_id': 'CSI001',
        'mean_daily_qty': 0.1,
        'std_daily_qty': 0.3,
        'z_value': 2.33,
        'lead_time_days': 30,
        'safety_stock': 5,
        'max_level': 10,
        'policy_class': 'CRITICAL_SLOW_IMPORTED'
    }])
    
    config = {
        'policy': {
            'forecast_blend_weight': 0.0,
            'review_period_days': 14,
            'sister_centres': {
                'count': 3,
                'transfer_lead_time_days': 1,
                'transfer_cost_factor': 0.05
            }
        }
    }
    
    engine = InventoryEngine(config, df_policy)
    
    # Override sister stock deterministic
    engine.sister_stock = {'CSI001': [1, 0, 0]} # One item available in sister 0
    
    # Test 1: Stock drops to 0, triggers reorder.
    # It should trigger SISTER_TRANSFER since 1 is available
    actions = engine.reorder_actions(pd.Timestamp('2023-01-01'), {'CSI001': 0})
    assert len(actions) == 1
    assert actions[0]['action_type'] == 'SISTER_TRANSFER'
    assert actions[0]['order_qty'] == 1 # Transfer usually just for 1 immediate need
    
    # Test 2: Sister stock should now be depleted
    assert engine.sister_stock['CSI001'] == [0, 0, 0]
    
    # Test 3: Next time stock is 0, it should trigger EMERGENCY_ORDER
    actions2 = engine.reorder_actions(pd.Timestamp('2023-01-02'), {'CSI001': 0})
    assert len(actions2) == 1
    assert actions2[0]['action_type'] == 'EMERGENCY_ORDER'
    assert actions2[0]['order_qty'] == 10 # Up to max level
