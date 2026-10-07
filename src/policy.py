import numpy as np
import pandas as pd
import sqlite3
import yaml

def load_config(config_path="config.yaml"):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def compute_policy_params(df_class, df_parts, review_period_days=14):
    """
    Computes static SS, ROP, Min, Max based on historical data.
    These can be used as a baseline or updated dynamically later.
    """
    df = df_class.copy()
    df = df.merge(df_parts[['sku_id', 'lead_time_days']], on='sku_id', how='left')
    
    # Intermittent items fallback: if std is 0 but mean > 0, use sqrt(mean) (Poisson approximation)
    df['sigma_d'] = np.where(df['std_daily_qty'] == 0, np.sqrt(df['mean_daily_qty']), df['std_daily_qty'])
    
    # Safety Stock: z * sigma_d * sqrt(LT)
    df['safety_stock'] = np.ceil(df['z_value'] * df['sigma_d'] * np.sqrt(df['lead_time_days']))
    
    # Reorder point: mean_daily_demand * LT + SS
    df['rop'] = np.ceil(df['mean_daily_qty'] * df['lead_time_days'] + df['safety_stock'])
    
    # Max Level: ROP + review_period_demand
    df['max_level'] = np.ceil(df['rop'] + df['mean_daily_qty'] * review_period_days)
    
    # Min Level: Safety Stock
    df['min_level'] = df['safety_stock']
    
    return df

class InventoryEngine:
    def __init__(self, config, df_policy, df_bookings=None):
        self.config = config
        self.df_policy = df_policy.set_index('sku_id')
        self.df_bookings = df_bookings if df_bookings is not None else pd.DataFrame()
        self.blend_weight = config['policy'].get('forecast_blend_weight', 0.7)
        self.review_period = config['policy'].get('review_period_days', 14)
        
        # Initialize sister centres mock stock for CRITICAL_SLOW_IMPORTED
        self.num_sister_centres = config['policy']['sister_centres']['count']
        self.sister_stock = self._init_sister_stock()
        
        # Service type to category probs (same as datagen for mock BOM)
        self.service_cat_probs = {
            'minor': {'filters': 0.8, 'fluids': 0.9, 'brakes': 0.05, 'electrical': 0.01, 'suspension': 0.0, 'engine': 0.0, 'body': 0.0, 'EV/high-voltage': 0.01},
            'major': {'filters': 0.95, 'fluids': 0.95, 'brakes': 0.4, 'electrical': 0.05, 'suspension': 0.1, 'engine': 0.1, 'body': 0.01, 'EV/high-voltage': 0.05},
            'repair': {'filters': 0.1, 'fluids': 0.2, 'brakes': 0.2, 'electrical': 0.4, 'suspension': 0.3, 'engine': 0.4, 'body': 0.05, 'EV/high-voltage': 0.1},
            'accident': {'filters': 0.05, 'fluids': 0.2, 'brakes': 0.1, 'electrical': 0.3, 'suspension': 0.5, 'engine': 0.2, 'body': 0.9, 'EV/high-voltage': 0.1}
        }

    def _init_sister_stock(self):
        """Give sister centres a small randomized stock of critical items."""
        sister_stock = {}
        np_random = np.random.RandomState(101)
        critical_skus = self.df_policy[self.df_policy['policy_class'] == 'CRITICAL_SLOW_IMPORTED'].index
        for sku in critical_skus:
            # Randomly 0 to 2 items per sister centre
            sister_stock[sku] = np_random.randint(0, 3, size=self.num_sister_centres).tolist()
        return sister_stock

    def get_blended_forecast(self, sku, current_date, lead_time):
        """
        Blends historical mean with booking-driven forecast over the lead time.
        For simplicity in this prototype, we approximate upcoming demand based on bookings.
        """
        hist_mean = self.df_policy.loc[sku, 'mean_daily_qty']
        
        # If no bookings data or no blend weight, rely on historical
        if self.df_bookings.empty or self.blend_weight == 0:
            return hist_mean * lead_time
            
        # Mock booking forecast: look ahead `lead_time` days
        end_date = current_date + pd.Timedelta(days=lead_time)
        upcoming = self.df_bookings[(self.df_bookings['date'] >= current_date) & (self.df_bookings['date'] < end_date)]
        
        if upcoming.empty:
            return hist_mean * lead_time
            
        # In a real system, we'd explode BOM. Here we approximate by checking if SKU matches upcoming service types.
        # This requires sku-level BOM mapping which we abstracted in datagen. 
        # For prototype simplicity, we assume the historical mean already captures the true BOM rate,
        # but we scale it by the ratio of (upcoming bookings / historical average bookings).
        
        avg_daily_bookings = len(self.df_bookings) / 365.0
        if avg_daily_bookings == 0:
            avg_daily_bookings = 1
            
        upcoming_daily_bookings = len(upcoming) / max(1, lead_time)
        booking_ratio = upcoming_daily_bookings / avg_daily_bookings
        
        forecasted_demand = hist_mean * booking_ratio * lead_time
        historical_demand = hist_mean * lead_time
        
        blended = self.blend_weight * forecasted_demand + (1 - self.blend_weight) * historical_demand
        return blended

    def try_sister_transfer(self, sku):
        """
        Attempts to transfer from a sister centre. Returns True if successful.
        """
        if sku not in self.sister_stock:
            return False
            
        stock_list = self.sister_stock[sku]
        for i in range(len(stock_list)):
            if stock_list[i] > 0:
                stock_list[i] -= 1 # Consume it
                return True
        return False

    def reorder_actions(self, current_date, current_inventory):
        """
        Evaluates inventory against policies and returns a list of reorder actions.
        current_inventory is a dict: {sku_id: physical_qty_available_plus_on_order}
        """
        actions = []
        for sku, row in self.df_policy.iterrows():
            qty_available = current_inventory.get(sku, 0)
            
            # Recalculate dynamic ROP based on blended forecast
            lead_time = row['lead_time_days']
            forecasted_lt_demand = self.get_blended_forecast(sku, current_date, lead_time)
            
            # Dynamic ROP = Forecasted LT Demand + Safety Stock
            dynamic_rop = np.ceil(forecasted_lt_demand + row['safety_stock'])
            
            if qty_available <= dynamic_rop:
                # Need to order
                # Order up to max level
                order_qty = max(1, int(row['max_level'] - qty_available))
                
                # If it's critically out of stock (<= 0), and it's CSI, try pooling first
                action_type = "STANDARD_ORDER"
                lead_time_applied = lead_time
                
                if qty_available <= 0 and row['policy_class'] == 'CRITICAL_SLOW_IMPORTED':
                    if self.try_sister_transfer(sku):
                        action_type = "SISTER_TRANSFER"
                        lead_time_applied = self.config['policy']['sister_centres']['transfer_lead_time_days']
                        order_qty = 1 # Transfers are usually just for the immediate need
                    else:
                        action_type = "EMERGENCY_ORDER"
                        # Emergency orders might have shorter lead times in reality, but cost more
                        # We'll use local lead time approximation or just standard LT
                
                actions.append({
                    'sku_id': sku,
                    'action_type': action_type,
                    'order_qty': order_qty,
                    'lead_time_days': lead_time_applied,
                    'trigger_rop': dynamic_rop,
                    'qty_available': qty_available,
                    'reason': f"Available ({qty_available}) <= ROP ({dynamic_rop})"
                })
                
        return actions

def main():
    config = load_config()
    db_path = 'data/inventory.db'
    conn = sqlite3.connect(db_path)
    
    df_class = pd.read_sql('SELECT * FROM sku_classification', conn)
    df_parts = pd.read_sql('SELECT * FROM parts_master', conn)
    try:
        df_bookings = pd.read_sql('SELECT * FROM bookings', conn)
        df_bookings['date'] = pd.to_datetime(df_bookings['date'])
    except:
        df_bookings = None
        
    df_policy = compute_policy_params(df_class, df_parts, config['policy']['review_period_days'])
    df_policy.to_sql('sku_policy', conn, index=False, if_exists='replace')
    conn.close()
    
    print("Policy computation complete. Saved to sku_policy table.")

if __name__ == "__main__":
    main()
