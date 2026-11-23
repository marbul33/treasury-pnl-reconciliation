from pathlib import Path

# Project paths
ROOT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT_DIR / "data" / "raw"
MART_DIR = ROOT_DIR / "data" / "mart"
SQL_DIR = ROOT_DIR / "sql"
DB_PATH = MART_DIR / "treasury.duckdb"

# Synthetic scenario parameters (NordicDesk Labs S.L.)
RANDOM_SEED = 42
COMPANY_NAME = "NordicDesk Labs S.L."
START_DATE = "2025-10-01"
END_DATE = "2026-03-31"
REPORTING_CURRENCY = "EUR"
INITIAL_CASH_EUR = 42_000.00  # Consolidated opening cash balance as of 2025-10-01

# Tax rules and reconciliation tolerances
EU_VAT_RATE = 0.21
PAYOUT_WINDOW_DAYS = 4  # Max expected settlement window between Stripe and bank
B2B_MATCH_WINDOW_DAYS = (
    5  # Fuzzy matching window (amount/date) when exact ref is missing
)
FX_TOLERANCE_PCT = 0.015  # Max 1.5% FX spread absorbed as financial expense
FX_TOLERANCE_ABS_EUR = 5.00  # Or up to €5.00 variance on cross-currency transfers
RUNWAY_ALERT_MONTHS = 3.0  # Critical liquidity runway threshold
