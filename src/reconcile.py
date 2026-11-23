"""
Phase 2: DuckDB Treasury Reconciliation & P&L Pipeline Runner.

Executes modular SQL transformations (Staging -> Reconciliation -> Marts),
persists the local DuckDB warehouse, and exports analytical marts to Parquet
for fast Quarto dashboard rendering.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from src.config import (
    B2B_MATCH_WINDOW_DAYS,
    DB_PATH,
    END_DATE,
    FX_TOLERANCE_ABS_EUR,
    FX_TOLERANCE_PCT,
    INITIAL_CASH_EUR,
    MART_DIR,
    RAW_DIR,
    SQL_DIR,
)

SQL_PIPELINE = [
    "01_staging.sql",
    "02_reconciliation.sql",
    "03_marts.sql",
]

EXPORT_MARTS = [
    "mart_exceptions",
    "mart_monthly_pnl",
    "mart_monthly_cashflow",
    "mart_kpi_summary",
]


def _render_sql(sql_path: Path) -> str:
    """Inject config parameters into SQL templates with POSIX-safe Windows paths."""
    sql_text = sql_path.read_text(encoding="utf-8")
    replacements = {
        "{{RAW_DIR}}": RAW_DIR.as_posix(),
        "{{END_DATE}}": END_DATE,
        "{{INITIAL_CASH_EUR}}": f"{INITIAL_CASH_EUR:.2f}",
        "{{FX_TOLERANCE_PCT}}": f"{FX_TOLERANCE_PCT:.4f}",
        "{{FX_TOLERANCE_ABS_EUR}}": f"{FX_TOLERANCE_ABS_EUR:.2f}",
        "{{B2B_MATCH_WINDOW_DAYS}}": str(B2B_MATCH_WINDOW_DAYS),
    }
    for token, value in replacements.items():
        sql_text = sql_text.replace(token, value)
    return sql_text


def run_reconciliation() -> None:
    """Execute full DuckDB pipeline and export Parquet marts."""
    MART_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()  # Clean rebuild guarantees deterministic outputs

    con = duckdb.connect(str(DB_PATH))
    try:
        for script_name in SQL_PIPELINE:
            sql_script = _render_sql(SQL_DIR / script_name)
            con.execute(sql_script)

        # Export marts to Parquet (preserves exact DECIMAL/DATE types for Quarto)
        for table_name in EXPORT_MARTS:
            out_path = (MART_DIR / f"{table_name}.parquet").as_posix()
            con.execute(f"COPY {table_name} TO '{out_path}' (FORMAT PARQUET);")

        # Print concise executive readout
        kpi = con.execute("SELECT * FROM mart_kpi_summary").fetchdf().iloc[0]
        exc_breakdown = con.execute(
            "SELECT anomaly_type, COUNT(*) AS cnt, SUM(amount_eur) AS total_eur "
            "FROM mart_exceptions GROUP BY anomaly_type ORDER BY anomaly_type"
        ).fetchdf()

        print("=== Phase 2 Complete: DuckDB Reconciliation Summary ===")
        print(f"  Net Revenue (Ex-VAT) : {kpi['total_net_revenue_eur']:>12,.2f} EUR")
        print(f"  Total EBITDA         : {kpi['total_ebitda_eur']:>12,.2f} EUR")
        print(f"  Stripe Fees + FX     : {kpi['total_fees_and_fx_eur']:>12,.2f} EUR")
        print(f"  Ending Cash (Mar 31) : {kpi['ending_cash_eur']:>12,.2f} EUR")
        print(f"  Auto-Recon Rate      : {kpi['auto_recon_rate_pct']:>11.1f}%")
        print(
            f"  Discrepancies Found  : {int(kpi['total_exceptions_count']):>12d} ({kpi['total_discrepancies_eur']:,.2f} EUR)"
        )
        print("\n--- Exceptions Breakdown ---")
        print(exc_breakdown.to_string(index=False))
    finally:
        con.close()


if __name__ == "__main__":
    run_reconciliation()
