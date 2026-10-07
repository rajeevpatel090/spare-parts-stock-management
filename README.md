# Spare Parts Stock Management - Prototype

This is a working prototype for a case competition (Raam Group EPiC, Track 4: Spare-Parts Stock Management, Luxury / Mercedes-Benz service centre).

**Disclaimer: All data generated and used within this application is entirely synthetic.** 
This prototype does not use any real data from Raam Group or Mercedes-Benz. Simulated results are meant to demonstrate a proposed solution loop and should not be presented as real historical data.

## Project Structure
- `src/` - Core logic (datagen, classification, policy, ledger, simulation)
- `app/` - Streamlit dashboard
- `data/` - Synthetic data output
- `tests/` - Acceptance checks
- `docs/` - Architecture and diagrams

## How to Run
1. `pip install -r requirements.txt`
2. `streamlit run app/main.py`