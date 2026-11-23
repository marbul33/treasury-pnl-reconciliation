"""
Automated Accounting & Reconciliation Integrity Tests (pytest).

Validates mathematical cash conservation, zero Cartesian row inflation,
VAT exclusion from accrual P&L, and 100% precision/recall against the
seeded ground-truth anomaly manifest.
"""

from __future__ import annotations

import json
from decimal import Decimal

import duckdb
import pytest

from src.config import DB_PATH, INITIAL_CASH_EUR, RAW_DIR


@pytest.fixture(scope="module")
def db():
    """Open read-only connection to the built DuckDB warehouse."""
    assert DB_PATH.exists(), "Run `python -m src.reconcile` before running pytest."
    con = duckdb.connect(str(DB_PATH), read_only=True)
    yield con
    con.close()


def test_bank_cash_conservation_to_the_cent(db):
    """Opening cash + sum of all bank flows must equal final ending cash exactly."""
    bank_net = db.execute("SELECT SUM(amount_eur) FROM stg_bank_txns").fetchone()[0]
    ending_cash = db.execute(
        "SELECT ending_cash_eur FROM mart_monthly_cashflow ORDER BY month DESC LIMIT 1"
    ).fetchone()[0]

    expected = Decimal(f"{INITIAL_CASH_EUR:.2f}") + bank_net
    assert ending_cash == expected, (
        f"Cash mismatch: expected {expected}, got {ending_cash}"
    )


def test_monthly_cash_continuity(db):
    """Each month's opening cash must equal the previous month's ending cash."""
    rows = db.execute(
        "SELECT month, opening_cash_eur, net_cashflow_eur, ending_cash_eur "
        "FROM mart_monthly_cashflow ORDER BY month"
    ).fetchall()

    prev_ending = Decimal(f"{INITIAL_CASH_EUR:.2f}")
    for month, opening, net_cf, ending in rows:
        assert opening == prev_ending, (
            f"Break in continuity at {month}: {opening} != {prev_ending}"
        )
        assert opening + net_cf == ending, f"Row math error at {month}"
        prev_ending = ending


def test_no_cartesian_explosion_in_reconciliation(db):
    """Verify strict 1:1 matching with zero duplicated ERP or Bank transaction keys."""
    # Layer 1: each non-duplicate Stripe charge_id appears at most once
    dup_charges = db.execute(
        """
        SELECT charge_id, COUNT(*)
        FROM rec_layer1_erp_stripe
        WHERE recon_status <> 'DUPLICATE_ERP_INVOICE' AND charge_id IS NOT NULL
        GROUP BY charge_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    assert dup_charges == [], (
        f"Cartesian duplication detected in Layer 1: {dup_charges}"
    )

    # Layer 2B: each B2B ERP invoice and each B2B bank wire appears at most once
    dup_b2b_erp = db.execute(
        "SELECT erp_row_id, COUNT(*) FROM rec_layer2b_b2b_wires GROUP BY erp_row_id HAVING COUNT(*) > 1"
    ).fetchall()
    dup_b2b_bank = db.execute(
        "SELECT bank_txn_id, COUNT(*) FROM rec_layer2b_b2b_wires "
        "WHERE bank_txn_id IS NOT NULL GROUP BY bank_txn_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert dup_b2b_erp == [], f"Duplicated B2B ERP rows: {dup_b2b_erp}"
    assert dup_b2b_bank == [], f"Duplicated B2B Bank wires: {dup_b2b_bank}"


def test_pnl_excludes_vat_and_erp_duplicates(db):
    """Accrual P&L net revenue must equal ex-VAT subtotal of non-duplicate ERP rows."""
    pnl_net_rev = db.execute(
        "SELECT SUM(net_revenue_eur) FROM mart_monthly_pnl"
    ).fetchone()[0]
    erp_clean_subtotal = db.execute(
        "SELECT SUM(subtotal_eur) FROM stg_erp_invoices WHERE NOT is_duplicate_erp"
    ).fetchone()[0]
    erp_gross_with_vat = db.execute(
        "SELECT SUM(total_eur) FROM stg_erp_invoices WHERE NOT is_duplicate_erp"
    ).fetchone()[0]

    assert pnl_net_rev == erp_clean_subtotal
    assert pnl_net_rev < erp_gross_with_vat, (
        "VAT appears to have leaked into P&L revenue."
    )


def test_100pct_ground_truth_anomaly_detection(db):
    """Engine must detect 100% of seeded anomalies with zero false positives."""
    manifest = json.loads(
        (RAW_DIR / "ground_truth_anomalies.json").read_text(encoding="utf-8")
    )
    expected_pairs = {
        (item["anomaly_type"], item["reference_id"]) for item in manifest["items"]
    }

    detected_rows = db.execute(
        "SELECT anomaly_type, reference_id FROM mart_exceptions"
    ).fetchall()
    detected_pairs = {(row[0], row[1]) for row in detected_rows}

    missed = expected_pairs - detected_pairs
    false_positives = detected_pairs - expected_pairs

    assert missed == set(), f"Missed seeded anomalies: {missed}"
    assert false_positives == set(), f"False positives detected: {false_positives}"
    assert len(detected_pairs) == manifest["total_anomalies"]
