import pandas as pd
import numpy as np
import sqlite3
import yaml
import uuid
from datetime import datetime, timedelta

def load_config(config_path="config.yaml"):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

class StockLedger:
    def __init__(self, db_path, config):
        self.db_path = db_path
        self.config = config
        self.mode = config['ledger'].get('mode', 'LEGACY')
        self.untracked_share = config['ledger'].get('legacy_untracked_share', 0.15)
        self.prov_hours = config['ledger'].get('gated_provisional_hours', 4)
        
        # In-memory tracking for simulation performance
        self.stock = {} # {sku_id: {'book': qty, 'physical': qty, 'unit_cost': cost, 'abc': class, 'theft_risk': float}}
        self.provisional_cards = {} # {card_id: timestamp_opened}
        
        # Metrics
        self.events = []
        
        self._init_db()
        self._load_initial_stock()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS ledger_events (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT,
                sku_id TEXT,
                qty INTEGER,
                event_type TEXT,
                job_card_id TEXT,
                technician_id TEXT,
                counter_user TEXT,
                book_qty_after INTEGER,
                physical_qty_after INTEGER
            )
        ''')
        conn.commit()
        conn.close()

    def _load_initial_stock(self):
        conn = sqlite3.connect(self.db_path)
        try:
            df_stock = pd.read_sql('SELECT * FROM opening_stock', conn)
            df_parts = pd.read_sql('SELECT sku_id, unit_cost FROM parts_master', conn)
            df_class = pd.read_sql('SELECT sku_id, abc_class FROM sku_classification', conn)
            
            df = df_stock.merge(df_parts, on='sku_id', how='left')
            df = df.merge(df_class, on='sku_id', how='left')
            
            np_random = np.random.RandomState(42) # Consistent theft risk
            
            for _, row in df.iterrows():
                self.stock[row['sku_id']] = {
                    'book': row['book_qty'],
                    'physical': row['physical_qty'],
                    'unit_cost': row['unit_cost'],
                    'abc': row['abc_class'],
                    'theft_risk': np_random.uniform(0.1, 1.0)
                }
        except Exception as e:
            print(f"Warning: Could not load initial stock ({e})")
        finally:
            conn.close()

    def _record_event(self, timestamp, sku, qty, event_type, job_card_id, tech_id, counter_user):
        book = self.stock[sku]['book']
        physical = self.stock[sku]['physical']
        
        event = {
            'event_id': str(uuid.uuid4()),
            'timestamp': timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
            'sku_id': sku,
            'qty': qty,
            'event_type': event_type,
            'job_card_id': job_card_id,
            'technician_id': tech_id,
            'counter_user': counter_user,
            'book_qty_after': book,
            'physical_qty_after': physical
        }
        self.events.append(event)
        
        # Batch insert to DB periodically in a real app, here we just keep in memory for speed
        # and flush at the end

    def issue_part(self, timestamp, sku, qty, job_card_id=None, tech_id="T1", counter_user="C1"):
        if sku not in self.stock:
            return False, "SKU not found"
            
        if self.stock[sku]['physical'] < qty:
            return False, "Insufficient physical stock"
            
        recorded_job_card = job_card_id
        update_book = True
        
        if self.mode == 'LEGACY':
            # Verbal issue is allowed.
            if job_card_id is None:
                # Shrinkage happens here: a certain % of unlinked issues never get recorded in the book
                if np.random.random() < self.untracked_share:
                    update_book = False
        
        elif self.mode == 'GATED':
            # Must have a job card.
            if job_card_id is None:
                # Generate a provisional card
                recorded_job_card = f"PROV-{uuid.uuid4().hex[:6].upper()}"
                self.provisional_cards[recorded_job_card] = timestamp
        
        # Update physical
        self.stock[sku]['physical'] -= qty
        
        # Update book
        if update_book:
            self.stock[sku]['book'] -= qty
            
        self._record_event(timestamp, sku, -qty, 'ISSUE', recorded_job_card, tech_id, counter_user)
        return True, recorded_job_card

    def receive_part(self, timestamp, sku, qty, counter_user="C1"):
        if sku not in self.stock:
            return False
            
        self.stock[sku]['physical'] += qty
        self.stock[sku]['book'] += qty
        
        self._record_event(timestamp, sku, qty, 'RECEIPT', None, None, counter_user)
        return True

    def reconcile(self, timestamp, sku, counter_user="C1"):
        if sku not in self.stock:
            return 0
            
        book = self.stock[sku]['book']
        physical = self.stock[sku]['physical']
        variance = physical - book
        
        if variance != 0:
            self.stock[sku]['book'] = physical # Adjust book to match physical
            self._record_event(timestamp, sku, variance, 'COUNT_ADJUST', None, None, counter_user)
            
        return variance

    def detect_phantom_stock(self):
        """Returns SKUs where book says we have it, but physical is 0."""
        phantoms = []
        for sku, data in self.stock.items():
            if data['book'] > 0 and data['physical'] <= 0:
                phantoms.append({
                    'sku_id': sku,
                    'book_qty': data['book'],
                    'physical_qty': data['physical'],
                    'value': data['book'] * data['unit_cost']
                })
        return phantoms

    def get_daily_count_list(self):
        """Top 50 value-x-theft-risk parts"""
        items = []
        for sku, data in self.stock.items():
            value = data['book'] * data['unit_cost']
            risk_score = value * data['theft_risk']
            items.append({'sku': sku, 'risk_score': risk_score})
            
        items.sort(key=lambda x: x['risk_score'], reverse=True)
        return [item['sku'] for item in items[:50]]

    def get_weekly_count_list(self):
        """All A-class parts"""
        return [sku for sku, data in self.stock.items() if data['abc'] == 'A']

    def generate_monthly_report(self):
        df_events = pd.DataFrame(self.events)
        if df_events.empty:
            return {}
            
        issues = df_events[df_events['event_type'] == 'ISSUE']
        receipts = df_events[df_events['event_type'] == 'RECEIPT']
        adjusts = df_events[df_events['event_type'] == 'COUNT_ADJUST']
        
        total_issues = len(issues)
        linked_issues = len(issues[~issues['job_card_id'].isnull()])
        unlinked_issues = total_issues - linked_issues
        
        linked_pct = (linked_issues / total_issues * 100) if total_issues > 0 else 0
        
        # Shrinkage in value (negative adjustments)
        # We need unit cost for each sku
        df_costs = pd.DataFrame([{'sku_id': k, 'unit_cost': v['unit_cost']} for k,v in self.stock.items()])
        adjusts = adjusts.merge(df_costs, on='sku_id', how='left')
        
        shrinkage_qty = adjusts[adjusts['qty'] < 0]['qty'].sum() * -1
        shrinkage_value = (adjusts[adjusts['qty'] < 0]['qty'] * -1 * adjusts['unit_cost']).sum()
        
        total_inventory_value = sum(data['book'] * data['unit_cost'] for data in self.stock.values())
        shrinkage_pct = (shrinkage_value / total_inventory_value * 100) if total_inventory_value > 0 else 0
        
        return {
            'total_receipts': len(receipts),
            'total_issues': total_issues,
            'linked_issues_pct': linked_pct,
            'unlinked_issues_count': unlinked_issues,
            'shrinkage_value_inr': shrinkage_value,
            'shrinkage_pct': shrinkage_pct
        }

    def flush_events_to_db(self):
        if not self.events:
            return
        df = pd.DataFrame(self.events)
        conn = sqlite3.connect(self.db_path)
        df.to_sql('ledger_events', conn, index=False, if_exists='append')
        conn.close()
        self.events = []
