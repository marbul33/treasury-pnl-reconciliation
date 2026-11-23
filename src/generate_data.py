"""
Phase 1: Synthetic Financial Data Generator for NordicDesk Labs S.L.

Generates 6 months (Oct 2025 - Mar 2026) of realistic, dirty multi-currency
SME financial records across 4 CSV files plus a ground-truth anomaly manifest:
  1. fx_rates_ecb.csv         - Daily reference FX rates (EUR, USD, GBP)
  2. stripe_balance_txns.csv  - D2C charges, refunds, disputes, and batched payouts
  3. bank_revolut_wise.csv    - Multi-currency bank inflows (payouts, B2B) & outflows
  4. erp_invoices.csv         - Accrual-basis invoices & credit notes (with VAT)
  5. ground_truth_anomalies.json - Exact catalog of injected accounting errors
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import (
    END_DATE,
    EU_VAT_RATE,
    INITIAL_CASH_EUR,
    RANDOM_SEED,
    RAW_DIR,
    START_DATE,
)


@dataclass
class AnomalyRecord:
    """Tracks each intentionally injected accounting error for automated QA."""

    anomaly_type: str
    severity: str
    reference_id: str
    currency: str
    amount_orig: float
    amount_eur_approx: float
    description: str


# ---------------------------------------------------------------------------
# 1. FX Reference Table (Daily ECB-style rates, 100% offline reproducible)
# ---------------------------------------------------------------------------
def generate_fx_rates(
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, dict[tuple[str, str], float]]:
    """Generate daily EUR-per-unit FX rates covering buffer dates around the period."""
    start = date.fromisoformat(START_DATE) - timedelta(days=7)
    end = date.fromisoformat(END_DATE) + timedelta(days=10)
    num_days = (end - start).days + 1

    # Realistic random walk for USD and GBP against EUR
    usd_steps = rng.normal(loc=0.0, scale=0.0025, size=num_days)
    gbp_steps = rng.normal(loc=0.0, scale=0.0020, size=num_days)

    usd_rates = np.clip(0.9200 + np.cumsum(usd_steps), 0.8850, 0.9600)
    gbp_rates = np.clip(1.1750 + np.cumsum(gbp_steps), 1.1350, 1.2150)

    rows: list[dict] = []
    lookup: dict[tuple[str, str], float] = {}

    for i in range(num_days):
        d_str = (start + timedelta(days=i)).isoformat()
        daily_map = {
            "EUR": 1.000000,
            "USD": round(float(usd_rates[i]), 6),
            "GBP": round(float(gbp_rates[i]), 6),
        }
        for ccy, rate in daily_map.items():
            rows.append({"rate_date": d_str, "currency": ccy, "eur_per_unit": rate})
            lookup[(d_str, ccy)] = rate

    return pd.DataFrame(rows), lookup


def _messy_ref(invoice_number: str, rng: np.random.Generator) -> str:
    """Inject realistic formatting noise into references (spaces, slashes, casing)."""
    style = int(rng.integers(0, 4))
    if style == 0:
        return invoice_number.replace("-", " ")
    if style == 1:
        return invoice_number.replace("-", "/")
    if style == 2:
        return invoice_number.lower().replace("-", "")
    return f"  {invoice_number}  "


def _account_for_currency(currency: str) -> str:
    """Map operating currency to its corresponding neo-bank account."""
    return {
        "EUR": "Revolut EUR",
        "USD": "Wise USD",
        "GBP": "Wise GBP",
    }[currency]


# ---------------------------------------------------------------------------
# 2. Core Business Simulation (D2C Stripe + B2B Wires + Operating Outflows)
# ---------------------------------------------------------------------------
def build_datasets(rng: np.random.Generator, fx_lookup: dict[tuple[str, str], float]):
    """Simulate clean business events first, then inject realistic noise and anomalies."""
    end_dt = date.fromisoformat(END_DATE)
    anomalies: list[AnomalyRecord] = []

    erp_rows: list[dict] = []
    stripe_rows: list[dict] = []
    bank_rows: list[dict] = []

    erp_id_seq = 1
    bank_id_seq = 1
    stripe_txn_seq = 1
    stripe_chg_seq = 1

    # Product catalog (ex-VAT prices in EUR): standing desks, monitor arms, chairs
    catalog_eur = [189.0, 249.0, 399.0, 549.0, 689.0]
    catalog_probs = [0.22, 0.23, 0.25, 0.18, 0.12]

    # Monthly D2C volume: Black Friday / Christmas spike followed by Q1 dip
    monthly_d2c_orders = [
        ("2025-10", 31, 95),
        ("2025-11", 30, 145),
        ("2025-12", 31, 155),
        ("2026-01", 31, 85),
        ("2026-02", 28, 80),
        ("2026-03", 31, 90),
    ]

    d2c_inv_seq = {"2025": 1, "2026": 1}
    d2c_order_records: list[dict] = []

    # --- A. Generate D2C Orders (Stripe + ERP) ---
    for ym, days_in_month, order_count in monthly_d2c_orders:
        year_str = ym[:4]
        order_days = np.sort(rng.integers(1, days_in_month + 1, size=order_count))

        for day in order_days:
            order_dt = date.fromisoformat(f"{ym}-{int(day):02d}")
            order_str = order_dt.isoformat()

            # Currency & geography mix: 65% EU (21% VAT), 22% US (0%), 13% UK (0%)
            ccy = str(rng.choice(["EUR", "USD", "GBP"], p=[0.65, 0.22, 0.13]))
            if ccy == "EUR":
                country = str(
                    rng.choice(["ES", "DE", "FR", "NL"], p=[0.50, 0.25, 0.15, 0.10])
                )
                vat_rate = EU_VAT_RATE
            elif ccy == "USD":
                country = "US"
                vat_rate = 0.0
            else:
                country = "GB"
                vat_rate = 0.0

            base_eur = float(rng.choice(catalog_eur, p=catalog_probs))
            rate = fx_lookup[(order_str, ccy)]
            subtotal = round(base_eur / rate, 2)
            vat_amount = round(subtotal * vat_rate, 2)
            total_amount = round(subtotal + vat_amount, 2)

            # Stripe European pricing: domestic EUR cheaper than cross-border USD/GBP
            fee_pct, fee_fixed = (0.015, 0.25) if ccy == "EUR" else (0.029, 0.30)
            fee_amount = round(total_amount * fee_pct + fee_fixed, 2)
            net_amount = round(total_amount - fee_amount, 2)

            inv_num = f"INV-{year_str}-{d2c_inv_seq[year_str]:04d}"
            d2c_inv_seq[year_str] += 1

            charge_id = f"ch_{stripe_chg_seq:06d}"
            stripe_chg_seq += 1
            txn_id = f"txn_{stripe_txn_seq:06d}"
            stripe_txn_seq += 1

            # Inject dirty reference formatting on ~28% of Stripe metadata (normal noise)
            stripe_ref = _messy_ref(inv_num, rng) if rng.random() < 0.28 else inv_num

            erp_row = {
                "erp_row_id": f"ERP-{erp_id_seq:05d}",
                "invoice_number": inv_num,
                "issue_date": order_str,
                "due_date": order_str,
                "channel": "D2C_STRIPE",
                "customer_name": f"D2C Customer #{stripe_chg_seq}",
                "customer_country": country,
                "currency": ccy,
                "subtotal_amount": subtotal,
                "vat_rate": vat_rate,
                "vat_amount": vat_amount,
                "total_amount": total_amount,
                "external_ref": charge_id,
                "status": "ISSUED",
            }
            erp_id_seq += 1
            erp_rows.append(erp_row)

            stripe_row = {
                "txn_id": txn_id,
                "charge_id": charge_id,
                "payout_id": "",  # Assigned during payout batching below
                "created_at": order_str,
                "available_on": "",
                "type": "charge",
                "currency": ccy,
                "gross_amount": total_amount,
                "fee_amount": fee_amount,
                "net_amount": net_amount,
                "invoice_ref": stripe_ref,
                "customer_country": country,
            }
            stripe_rows.append(stripe_row)
            d2c_order_records.append(
                {
                    "order_dt": order_dt,
                    "erp_row": erp_row,
                    "stripe_row": stripe_row,
                    "refunded": False,
                }
            )

    # --- B. Generate Realistic Refunds (~4%) and Resolved Disputes (2 cases) ---
    # Restrict to orders before Mar 18 so refunds settle cleanly before quarter end
    eligible_for_refund = [
        rec
        for rec in d2c_order_records
        if rec["order_dt"] <= date(2026, 3, 18) and rec["erp_row"]["currency"] == "EUR"
    ]
    refund_indices = rng.choice(len(eligible_for_refund), size=24, replace=False)

    for idx_pos, idx in enumerate(refund_indices):
        rec = eligible_for_refund[int(idx)]
        rec["refunded"] = True
        orig_erp = rec["erp_row"]
        orig_stripe = rec["stripe_row"]

        event_dt = rec["order_dt"] + timedelta(days=int(rng.integers(3, 11)))
        event_str = event_dt.isoformat()
        is_dispute = idx_pos < 2  # First 2 are handled chargebacks/disputes

        # Stripe does not refund processing fees; disputes incur an extra 15 EUR fee
        event_fee = 15.00 if is_dispute else 0.00
        gross_neg = round(-orig_erp["total_amount"], 2)
        net_neg = round(gross_neg - event_fee, 2)

        txn_id = f"txn_{stripe_txn_seq:06d}"
        stripe_txn_seq += 1

        stripe_rows.append(
            {
                "txn_id": txn_id,
                "charge_id": orig_stripe["charge_id"],
                "payout_id": "",
                "created_at": event_str,
                "available_on": "",
                "type": "dispute" if is_dispute else "refund",
                "currency": orig_erp["currency"],
                "gross_amount": gross_neg,
                "fee_amount": event_fee,
                "net_amount": net_neg,
                "invoice_ref": orig_erp["invoice_number"],
                "customer_country": orig_erp["customer_country"],
            }
        )

        # Matching ERP Credit Note keeps accrual P&L and VAT clean
        cn_year = event_str[:4]
        cn_num = f"CN-{cn_year}-{d2c_inv_seq[cn_year]:04d}"
        d2c_inv_seq[cn_year] += 1

        erp_rows.append(
            {
                "erp_row_id": f"ERP-{erp_id_seq:05d}",
                "invoice_number": cn_num,
                "issue_date": event_str,
                "due_date": event_str,
                "channel": "D2C_STRIPE",
                "customer_name": orig_erp["customer_name"],
                "customer_country": orig_erp["customer_country"],
                "currency": orig_erp["currency"],
                "subtotal_amount": round(-orig_erp["subtotal_amount"], 2),
                "vat_rate": orig_erp["vat_rate"],
                "vat_amount": round(-orig_erp["vat_amount"], 2),
                "total_amount": round(-orig_erp["total_amount"], 2),
                "external_ref": orig_stripe["charge_id"],
                "status": "CREDIT_NOTE",
            }
        )
        erp_id_seq += 1

    # --- C. Group Stripe Balance Txns into Batched Payouts (T+2 Bank Lag) ---
    # EUR batches every 3 days; lower-volume USD/GBP batch every 7 days
    start_dt = date.fromisoformat(START_DATE)
    payout_batches: dict[tuple[str, int], list[dict]] = {}

    for s_row in stripe_rows:
        c_dt = date.fromisoformat(s_row["created_at"])
        # Last 3 days of March remain unbatched in Stripe (normal IN_TRANSIT noise)
        if c_dt >= date(2026, 3, 29):
            s_row["payout_id"] = ""
            s_row["available_on"] = (c_dt + timedelta(days=3)).isoformat()
            continue

        step = 3 if s_row["currency"] == "EUR" else 7
        bucket = (c_dt - start_dt).days // step
        payout_batches.setdefault((s_row["currency"], bucket), []).append(s_row)

    settled_payout_bank_rows: list[dict] = []

    for (ccy, bucket), batch_rows in sorted(payout_batches.items()):
        max_dt = max(date.fromisoformat(r["created_at"]) for r in batch_rows)
        arrival_dt = max_dt + timedelta(days=2)  # Realistic T+2 bank settlement delay
        payout_id = f"po_{ccy.lower()}_{max_dt.strftime('%Y%m%d')}_{bucket:02d}"

        batch_net = round(sum(r["net_amount"] for r in batch_rows), 2)
        for r in batch_rows:
            r["payout_id"] = payout_id
            r["available_on"] = arrival_dt.isoformat()

        # If bank arrival falls in April, it is IN_TRANSIT as of March 31
        if arrival_dt > end_dt or batch_net <= 0:
            continue

        b_row = {
            "bank_txn_id": f"BNK-{bank_id_seq:05d}",
            "account_name": _account_for_currency(ccy),
            "booking_date": arrival_dt.isoformat(),
            "value_date": arrival_dt.isoformat(),
            "direction": "IN",
            "category_hint": "STRIPE_PAYOUT",
            "currency": ccy,
            "amount": batch_net,
            "description": f"STRIPE PAYOUT {payout_id.upper()}",
        }
        bank_id_seq += 1
        bank_rows.append(b_row)
        settled_payout_bank_rows.append({"payout_id": payout_id, "bank_row": b_row})

    # --- D. Generate B2B Invoices & Bank Wires (includes FX spread & overdue cases) ---
    b2b_clients = [
        ("WeWork Iberia S.L.", "ES", "EUR", EU_VAT_RATE),
        ("Krono Office GmbH", "DE", "EUR", EU_VAT_RATE),
        ("Atelier Nordique SAS", "FR", "EUR", EU_VAT_RATE),
        ("Apex Workspace LLC", "US", "USD", 0.0),
        ("Thames Ergonomics Ltd", "GB", "GBP", 0.0),
    ]
    b2b_seq = {"2025": 1, "2026": 1}
    b2b_records: list[dict] = []

    for ym, days_in_month, _ in monthly_d2c_orders:
        year_str = ym[:4]
        for slot in range(5):
            issue_day = min(days_in_month, 3 + slot * 5 + int(rng.integers(0, 3)))
            issue_dt = date.fromisoformat(f"{ym}-{issue_day:02d}")
            due_dt = issue_dt + timedelta(days=30)

            client_name, country, ccy, vat_rate = b2b_clients[slot % len(b2b_clients)]
            rate = fx_lookup[(issue_dt.isoformat(), ccy)]
            subtotal_eur = float(rng.choice([2400.0, 3200.0, 4100.0, 5200.0]))
            subtotal = round(subtotal_eur / rate, 2)
            vat_amount = round(subtotal * vat_rate, 2)
            total_amount = round(subtotal + vat_amount, 2)

            inv_num = f"B2B-{year_str}-{b2b_seq[year_str]:04d}"
            b2b_seq[year_str] += 1

            erp_row = {
                "erp_row_id": f"ERP-{erp_id_seq:05d}",
                "invoice_number": inv_num,
                "issue_date": issue_dt.isoformat(),
                "due_date": due_dt.isoformat(),
                "channel": "B2B_WIRE",
                "customer_name": client_name,
                "customer_country": country,
                "currency": ccy,
                "subtotal_amount": subtotal,
                "vat_rate": vat_rate,
                "vat_amount": vat_amount,
                "total_amount": total_amount,
                "external_ref": inv_num,
                "status": "ISSUED",
            }
            erp_id_seq += 1
            erp_rows.append(erp_row)
            b2b_records.append(
                {"issue_dt": issue_dt, "due_dt": due_dt, "erp_row": erp_row}
            )

    # Select exactly 3 overdue B2B invoices (due before Mar 15) to remain unpaid (Anomaly #4)
    overdue_candidates = [
        r for r in b2b_records if date(2025, 12, 1) <= r["due_dt"] <= date(2026, 3, 15)
    ]
    overdue_chosen = {
        overdue_candidates[1]["erp_row"]["invoice_number"],
        overdue_candidates[4]["erp_row"]["invoice_number"],
        overdue_candidates[7]["erp_row"]["invoice_number"],
    }

    fx_cross_counter = 0
    missing_ref_counter = 0

    for rec in b2b_records:
        inv = rec["erp_row"]
        inv_num = inv["invoice_number"]

        if inv_num in overdue_chosen:
            rate = fx_lookup[(inv["issue_date"], inv["currency"])]
            anomalies.append(
                AnomalyRecord(
                    anomaly_type="OVERDUE_B2B_INVOICE",
                    severity="MEDIUM",
                    reference_id=inv_num,
                    currency=inv["currency"],
                    amount_orig=inv["total_amount"],
                    amount_eur_approx=round(inv["total_amount"] * rate, 2),
                    description=f"B2B invoice past due ({inv['due_date']}) with no bank wire received.",
                )
            )
            continue

        # March invoices due in April remain legitimately OPEN_NOT_DUE (normal AR, not an anomaly)
        if rec["due_dt"] > end_dt and rec["issue_dt"] >= date(2026, 3, 15):
            continue

        pay_dt = min(
            end_dt, rec["issue_dt"] + timedelta(days=int(rng.integers(14, 29)))
        )
        pay_str = pay_dt.isoformat()
        ccy = inv["currency"]

        # Normal noise #1: 3 USD/GBP B2B wires settled into Revolut EUR with ~0.8% bank FX spread
        if ccy in ("USD", "GBP") and fx_cross_counter < 3:
            fx_cross_counter += 1
            ecb_rate = fx_lookup[(pay_str, ccy)]
            spread = 0.0085  # 0.85% bank FX spread (within 1.5% tolerance -> FX Cost, not anomaly)
            settled_eur = round(inv["total_amount"] * ecb_rate * (1.0 - spread), 2)
            bank_rows.append(
                {
                    "bank_txn_id": f"BNK-{bank_id_seq:05d}",
                    "account_name": "Revolut EUR",
                    "booking_date": pay_str,
                    "value_date": pay_str,
                    "direction": "IN",
                    "category_hint": "B2B_WIRE",
                    "currency": "EUR",
                    "amount": settled_eur,
                    "description": f"SWIFT WIRE {inv['customer_name'].upper()} REF {_messy_ref(inv_num, rng)}",
                }
            )
            bank_id_seq += 1
            continue

        # Normal noise #2: 3 same-currency wires omit invoice ref (forces Level-2 amount+date matching)
        if missing_ref_counter < 3 and ccy == "EUR":
            missing_ref_counter += 1
            desc = f"SEPA TRANSFER {inv['customer_name'].upper()} PAYMENT"
        else:
            ref_text = _messy_ref(inv_num, rng) if rng.random() < 0.4 else inv_num
            desc = f"WIRE TRANSFER {inv['customer_name'].upper()} {ref_text}"

        bank_rows.append(
            {
                "bank_txn_id": f"BNK-{bank_id_seq:05d}",
                "account_name": _account_for_currency(ccy),
                "booking_date": pay_str,
                "value_date": pay_str,
                "direction": "IN",
                "category_hint": "B2B_WIRE",
                "currency": ccy,
                "amount": inv["total_amount"],
                "description": desc,
            }
        )
        bank_id_seq += 1

    # --- E. Generate Realistic Operating Outflows (COGS, Ads, SaaS, Payroll, VAT) ---
    # Q1 reflects heavy spring inventory restock + Ads -> realistic Cash Runway tension
    monthly_outflows_eur = {
        "2025-10": {
            "COGS_SUPPLIER": 27500,
            "MARKETING_ADS": 11500,
            "SAAS_OPS": 3300,
            "PAYROLL": 13500,
        },
        "2025-11": {
            "COGS_SUPPLIER": 33000,
            "MARKETING_ADS": 18500,
            "SAAS_OPS": 3500,
            "PAYROLL": 13500,
        },
        "2025-12": {
            "COGS_SUPPLIER": 23000,
            "MARKETING_ADS": 17500,
            "SAAS_OPS": 3600,
            "PAYROLL": 14200,
        },
        "2026-01": {
            "COGS_SUPPLIER": 25000,
            "MARKETING_ADS": 13500,
            "SAAS_OPS": 3400,
            "PAYROLL": 13800,
        },
        "2026-02": {
            "COGS_SUPPLIER": 39500,
            "MARKETING_ADS": 14500,
            "SAAS_OPS": 3450,
            "PAYROLL": 13800,
        },
        "2026-03": {
            "COGS_SUPPLIER": 37000,
            "MARKETING_ADS": 15200,
            "SAAS_OPS": 3550,
            "PAYROLL": 13800,
        },
    }

    for ym, cats in monthly_outflows_eur.items():
        # Split COGS across EUR supplier (65%) and USD Asian supplier (35%)
        cogs_eur = round(cats["COGS_SUPPLIER"] * 0.65, 2)
        cogs_usd_d = f"{ym}-14"
        cogs_usd = round(
            (cats["COGS_SUPPLIER"] * 0.35) / fx_lookup[(cogs_usd_d, "USD")], 2
        )

        outflow_specs = [
            (
                f"{ym}-08",
                "EUR",
                "COGS_SUPPLIER",
                -cogs_eur,
                "SEPA SUPPLIER NORDIC FRAME MANUFACTURING",
            ),
            (
                cogs_usd_d,
                "USD",
                "COGS_SUPPLIER",
                -cogs_usd,
                "SWIFT SUPPLIER SHENZHEN ERGO HARDWARE",
            ),
            (
                f"{ym}-12",
                "EUR",
                "MARKETING_ADS",
                -round(cats["MARKETING_ADS"] * 0.60, 2),
                "META ADS IRELAND LTD",
            ),
            (
                f"{ym}-18",
                "USD",
                "MARKETING_ADS",
                -round(
                    (cats["MARKETING_ADS"] * 0.40) / fx_lookup[(f"{ym}-18", "USD")], 2
                ),
                "GOOGLE ADS PERFORMANCE",
            ),
            (
                f"{ym}-05",
                "EUR",
                "SAAS_OPS",
                -round(cats["SAAS_OPS"] * 0.55, 2),
                "GESTORIA FISCAL & SHOPIFY PLUS EU",
            ),
            (
                f"{ym}-15",
                "USD",
                "SAAS_OPS",
                -round((cats["SAAS_OPS"] * 0.45) / fx_lookup[(f"{ym}-15", "USD")], 2),
                "AWS CLOUD & HUBSPOT CRM",
            ),
            (
                f"{ym}-27",
                "EUR",
                "PAYROLL",
                -float(cats["PAYROLL"]),
                "NOMINAS EQUIPO & SEGURIDAD SOCIAL",
            ),
        ]

        for b_date, ccy, cat, amt, desc in outflow_specs:
            bank_rows.append(
                {
                    "bank_txn_id": f"BNK-{bank_id_seq:05d}",
                    "account_name": _account_for_currency(ccy),
                    "booking_date": b_date,
                    "value_date": b_date,
                    "direction": "OUT",
                    "category_hint": cat,
                    "currency": ccy,
                    "amount": amt,
                    "description": desc,
                }
            )
            bank_id_seq += 1

    # Q4 2025 Spanish VAT quarterly settlement paid on Jan 20, 2026 (Modelo 303)
    q4_vat_collected = sum(
        r["vat_amount"]
        for r in erp_rows
        if r["currency"] == "EUR" and "2025-10-01" <= r["issue_date"] <= "2025-12-31"
    )
    q4_vat_net_payable = round(
        q4_vat_collected * 0.62, 2
    )  # Net of deductible input VAT
    bank_rows.append(
        {
            "bank_txn_id": f"BNK-{bank_id_seq:05d}",
            "account_name": "Revolut EUR",
            "booking_date": "2026-01-20",
            "value_date": "2026-01-20",
            "direction": "OUT",
            "category_hint": "TAX_VAT",
            "currency": "EUR",
            "amount": -q4_vat_net_payable,
            "description": "AEAT MODELO 303 LIQUIDACION IVA 4T 2025",
        }
    )
    bank_id_seq += 1

    # -----------------------------------------------------------------------
    # 3. Inject Remaining 4 Anomaly Families (Total = 15 Ground Truth Errors)
    # -----------------------------------------------------------------------

    # Clean D2C pool (settled before Mar 20, not refunded) to avoid overlap with IN_TRANSIT
    clean_d2c_pool = [
        rec
        for rec in d2c_order_records
        if not rec["refunded"]
        and date(2025, 10, 10) <= rec["order_dt"] <= date(2026, 3, 18)
    ]

    # Anomaly #1: DUPLICATE_ERP_INVOICE (3 cases - webhook retry created duplicate invoice)
    dup_targets = [clean_d2c_pool[45], clean_d2c_pool[210], clean_d2c_pool[390]]
    for rec in dup_targets:
        orig = rec["erp_row"]
        dup_row = orig.copy()
        dup_row["erp_row_id"] = f"ERP-{erp_id_seq:05d}"
        erp_id_seq += 1
        dup_row["invoice_number"] = f"{orig['invoice_number']}-DUP"
        erp_rows.append(dup_row)

        rate = fx_lookup[(orig["issue_date"], orig["currency"])]
        anomalies.append(
            AnomalyRecord(
                anomaly_type="DUPLICATE_ERP_INVOICE",
                severity="HIGH",
                reference_id=dup_row["invoice_number"],
                currency=orig["currency"],
                amount_orig=orig["total_amount"],
                amount_eur_approx=round(orig["total_amount"] * rate, 2),
                description=f"Duplicate ERP invoice for Stripe charge {orig['external_ref']}.",
            )
        )

    # Anomaly #2: MISSING_ERP_INVOICE (4 cases - Stripe paid, ERP failed to issue invoice)
    missing_targets = [
        clean_d2c_pool[20],
        clean_d2c_pool[155],
        clean_d2c_pool[320],
        clean_d2c_pool[480],
    ]
    missing_erp_ids = {rec["erp_row"]["erp_row_id"] for rec in missing_targets}
    erp_rows = [r for r in erp_rows if r["erp_row_id"] not in missing_erp_ids]

    for rec in missing_targets:
        orig = rec["erp_row"]
        rate = fx_lookup[(orig["issue_date"], orig["currency"])]
        anomalies.append(
            AnomalyRecord(
                anomaly_type="MISSING_ERP_INVOICE",
                severity="HIGH",
                reference_id=orig["external_ref"],
                currency=orig["currency"],
                amount_orig=orig["total_amount"],
                amount_eur_approx=round(orig["total_amount"] * rate, 2),
                description=f"Settled Stripe charge {orig['external_ref']} missing corresponding ERP invoice.",
            )
        )

    # Anomaly #3: UNMATCHED_PAYOUT_DELTA (2 cases - Bank received less than Stripe payout sum)
    eur_payouts = [
        p
        for p in settled_payout_bank_rows
        if p["bank_row"]["currency"] == "EUR" and p["bank_row"]["amount"] > 2000
    ]
    usd_payouts = [
        p
        for p in settled_payout_bank_rows
        if p["bank_row"]["currency"] == "USD" and p["bank_row"]["amount"] > 1200
    ]

    delta_specs = [
        (eur_payouts[len(eur_payouts) // 3], 340.00),
        (usd_payouts[len(usd_payouts) // 2], 485.00),
    ]
    for payout_obj, withheld_amt in delta_specs:
        b_row = payout_obj["bank_row"]
        b_row["amount"] = round(b_row["amount"] - withheld_amt, 2)
        rate = fx_lookup[(b_row["booking_date"], b_row["currency"])]
        anomalies.append(
            AnomalyRecord(
                anomaly_type="UNMATCHED_PAYOUT_DELTA",
                severity="CRITICAL",
                reference_id=payout_obj["payout_id"],
                currency=b_row["currency"],
                amount_orig=withheld_amt,
                amount_eur_approx=round(withheld_amt * rate, 2),
                description=f"Bank payout {payout_obj['payout_id']} short by {withheld_amt:.2f} {b_row['currency']} vs Stripe ledger.",
            )
        )

    # Anomaly #5: UNCLASSIFIED_BANK_OUTFLOW (3 cases - Suspicious/unmatched bank debits)
    unclassified_specs = [
        ("2025-11-19", "EUR", -299.00, "CARD DEBIT CLOUDSVC PRO DUPLICATE CHARGE"),
        ("2026-01-28", "USD", -640.00, "WIRE DEBIT UNRECOGNIZED VENDOR #8814"),
        ("2026-03-11", "GBP", -185.00, "FPS DEBIT REF MISSING - REVIEW REQUIRED"),
    ]
    for u_date, ccy, amt, desc in unclassified_specs:
        b_id = f"BNK-{bank_id_seq:05d}"
        bank_id_seq += 1
        bank_rows.append(
            {
                "bank_txn_id": b_id,
                "account_name": _account_for_currency(ccy),
                "booking_date": u_date,
                "value_date": u_date,
                "direction": "OUT",
                "category_hint": "UNKNOWN",
                "currency": ccy,
                "amount": amt,
                "description": desc,
            }
        )
        rate = fx_lookup[(u_date, ccy)]
        anomalies.append(
            AnomalyRecord(
                anomaly_type="UNCLASSIFIED_BANK_OUTFLOW",
                severity="MEDIUM",
                reference_id=b_id,
                currency=ccy,
                amount_orig=abs(amt),
                amount_eur_approx=round(abs(amt) * rate, 2),
                description=f"Unclassified bank outflow ({desc}) without vendor receipt.",
            )
        )

    return (
        pd.DataFrame(stripe_rows).sort_values(["created_at", "txn_id"]),
        pd.DataFrame(bank_rows).sort_values(["booking_date", "bank_txn_id"]),
        pd.DataFrame(erp_rows).sort_values(["issue_date", "erp_row_id"]),
        anomalies,
    )


def main() -> None:
    """Run synthetic data pipeline and export CSVs + ground-truth anomaly manifest."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(RANDOM_SEED)

    fx_df, fx_lookup = generate_fx_rates(rng)
    stripe_df, bank_df, erp_df, anomalies = build_datasets(rng, fx_lookup)

    # Export CSVs with explicit UTF-8 encoding for Windows compatibility
    fx_df.to_csv(RAW_DIR / "fx_rates_ecb.csv", index=False, encoding="utf-8")
    stripe_df.to_csv(RAW_DIR / "stripe_balance_txns.csv", index=False, encoding="utf-8")
    bank_df.to_csv(RAW_DIR / "bank_revolut_wise.csv", index=False, encoding="utf-8")
    erp_df.to_csv(RAW_DIR / "erp_invoices.csv", index=False, encoding="utf-8")

    # Save ground-truth anomaly manifest for automated testing in Phase 2
    manifest = {
        "total_anomalies": len(anomalies),
        "counts_by_type": pd.Series([a.anomaly_type for a in anomalies])
        .value_counts()
        .to_dict(),
        "items": [asdict(a) for a in anomalies],
    }
    manifest_path = Path(RAW_DIR / "ground_truth_anomalies.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Quick financial sanity readout (EUR approx)
    bank_eur_net = sum(
        row["amount"] * fx_lookup[(row["booking_date"], row["currency"])]
        for _, row in bank_df.iterrows()
    )
    ending_cash = round(INITIAL_CASH_EUR + bank_eur_net, 2)

    print("=== Phase 1 Complete: Synthetic Raw Data Generated ===")
    print(f"  fx_rates_ecb.csv        : {len(fx_df):>5} rows")
    print(f"  stripe_balance_txns.csv : {len(stripe_df):>5} rows")
    print(f"  bank_revolut_wise.csv   : {len(bank_df):>5} rows")
    print(f"  erp_invoices.csv        : {len(erp_df):>5} rows")
    print(
        f"  Seeded anomalies        : {len(anomalies):>5} ({manifest['counts_by_type']})"
    )
    print(
        f"  Cash check (EUR approx) : Start {INITIAL_CASH_EUR:,.2f} -> End {ending_cash:,.2f}"
    )


if __name__ == "__main__":
    main()
