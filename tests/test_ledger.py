import os
import pytest
import sqlite3
import pandas as pd
from datetime import datetime
from src.ledger import StockLedger

@pytest.fixture
def mock_db():
    db_path = 'test_inventory.db'
    if os.path.exists(db_path):
        os.remove(db_path)
    
    # Setup dummy data
    conn = sqlite3.connect(db_path)
    pd.DataFrame({
        'sku_id': ['SKU1', 'SKU2'],
        'opening_qty': [10, 10],
        'value': [1000, 2000],
        'book_qty': [10, 10],
        'physical_qty': [10, 10]
    }).to_sql('opening_stock', conn, index=False)
    
    pd.DataFrame({
        'sku_id': ['SKU1', 'SKU2'],
        'unit_cost': [100.0, 200.0]
    }).to_sql('parts_master', conn, index=False)
    
    pd.DataFrame({
        'sku_id': ['SKU1', 'SKU2'],
        'abc_class': ['A', 'B']
    }).to_sql('sku_classification', conn, index=False)
    
    conn.close()
    
    yield db_path
    
    if os.path.exists(db_path):
        os.remove(db_path)

def test_gated_mode_provisionalises_unlinked(mock_db):
    config = {'ledger': {'mode': 'GATED'}}
    ledger = StockLedger(mock_db, config)
    
    now = datetime.now()
    success, job_card = ledger.issue_part(now, 'SKU1', 1, job_card_id=None)
    
    assert success
    assert job_card.startswith('PROV-')
    assert job_card in ledger.provisional_cards
    
    report = ledger.generate_monthly_report()
    assert report['linked_issues_pct'] == 100.0 # Provisional counts as linked
    assert report['unlinked_issues_count'] == 0

def test_phantom_stock_detection(mock_db):
    config = {'ledger': {'mode': 'LEGACY', 'legacy_untracked_share': 1.0}} # Force 100% untracked
    ledger = StockLedger(mock_db, config)
    
    # Issue without job card in legacy mode -> physical drops, book doesn't (due to 1.0 untracked share)
    # Let's issue all 10 physical items
    now = datetime.now()
    ledger.issue_part(now, 'SKU2', 10, job_card_id=None)
    
    # Now physical should be 0, book should be 10
    assert ledger.stock['SKU2']['physical'] == 0
    assert ledger.stock['SKU2']['book'] == 10
    
    phantoms = ledger.detect_phantom_stock()
    assert len(phantoms) == 1
    assert phantoms[0]['sku_id'] == 'SKU2'
    assert phantoms[0]['book_qty'] == 10
    assert phantoms[0]['physical_qty'] == 0

def test_legacy_mode_allows_unlinked(mock_db):
    config = {'ledger': {'mode': 'LEGACY', 'legacy_untracked_share': 0.0}}
    ledger = StockLedger(mock_db, config)
    
    now = datetime.now()
    success, job_card = ledger.issue_part(now, 'SKU1', 1, job_card_id=None)
    
    assert success
    assert job_card is None # Unlinked
    
    report = ledger.generate_monthly_report()
    assert report['linked_issues_pct'] == 0.0
    assert report['unlinked_issues_count'] == 1
