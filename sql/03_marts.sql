-- ============================================================================
-- 03_marts.sql
-- Builds executive reporting marts consumed directly by the Quarto dashboard:
--   1. mart_exceptions       - Actionable anomaly queue for accounting review
--   2. mart_monthly_pnl      - Accrual-basis P&L (ex-VAT, EUR) for Waterfall
--   3. mart_monthly_cashflow - Consolidated EUR liquidity & Base/Stress Runway
--   4. mart_kpi_summary      - Top-level KPI card metrics
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. ACTIONABLE EXCEPTIONS QUEUE (All 5 anomaly families)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_exceptions AS
WITH raw_exceptions AS (
    -- #1: Duplicate ERP invoices
    SELECT
        event_date,
        event_month AS month,
        'ERP <-> Stripe' AS source_layer,
        'DUPLICATE_ERP_INVOICE' AS anomaly_type,
        'HIGH' AS severity,
        invoice_number AS reference_id,
        currency,
        erp_total_orig AS amount_orig,
        amount_eur,
        'ERP Billing Impact' AS impact_bucket,
        'Issue credit note / void duplicate invoice in ERP to avoid overpaying VAT.' AS recommended_action
    FROM rec_layer1_erp_stripe
    WHERE recon_status = 'DUPLICATE_ERP_INVOICE'

    UNION ALL

    -- #2: Missing ERP invoices (cash collected in Stripe without invoice)
    SELECT
        event_date,
        event_month AS month,
        'ERP <-> Stripe' AS source_layer,
        'MISSING_ERP_INVOICE' AS anomaly_type,
        'HIGH' AS severity,
        charge_id AS reference_id,
        currency,
        stripe_gross_orig AS amount_orig,
        amount_eur,
        'ERP Billing Impact' AS impact_bucket,
        'Issue missing customer invoice in ERP to stay tax-compliant.' AS recommended_action
    FROM rec_layer1_erp_stripe
    WHERE recon_status = 'MISSING_ERP_INVOICE'

    UNION ALL

    -- #3: Unmatched Stripe payout delta (shortfall in bank vs Stripe ledger)
    SELECT
        event_date,
        event_month AS month,
        'Stripe <-> Bank' AS source_layer,
        'UNMATCHED_PAYOUT_DELTA' AS anomaly_type,
        'CRITICAL' AS severity,
        payout_id AS reference_id,
        currency,
        delta_orig AS amount_orig,
        delta_eur AS amount_eur,
        'Cash Impact' AS impact_bucket,
        'Investigate withheld funds or unrecorded chargeback in Stripe dashboard.' AS recommended_action
    FROM rec_layer2a_stripe_payouts
    WHERE recon_status = 'UNMATCHED_PAYOUT_DELTA'

    UNION ALL

    -- #4: Overdue B2B invoices past due date with no wire received
    SELECT
        due_date AS event_date,
        STRFTIME(due_date, '%Y-%m') AS month,
        'B2B ERP <-> Bank' AS source_layer,
        'OVERDUE_B2B_INVOICE' AS anomaly_type,
        'MEDIUM' AS severity,
        invoice_number AS reference_id,
        erp_currency AS currency,
        erp_total_orig AS amount_orig,
        erp_total_eur AS amount_eur,
        'AR / Collections' AS impact_bucket,
        'Trigger accounts receivable follow-up with client (' || customer_name || ').' AS recommended_action
    FROM rec_layer2b_b2b_wires
    WHERE recon_status = 'OVERDUE_B2B_INVOICE'

    UNION ALL

    -- #5: Unclassified / suspicious bank outflows
    SELECT
        booking_date AS event_date,
        booking_month AS month,
        'Bank Outflows' AS source_layer,
        'UNCLASSIFIED_BANK_OUTFLOW' AS anomaly_type,
        'MEDIUM' AS severity,
        bank_txn_id AS reference_id,
        currency,
        ABS(amount) AS amount_orig,
        ABS(amount_eur) AS amount_eur,
        'Cash Impact' AS impact_bucket,
        'Request missing vendor receipt or dispute duplicate charge (' || description || ').' AS recommended_action
    FROM stg_bank_txns
    WHERE direction = 'OUT'
      AND category_hint = 'UNKNOWN'
)
SELECT
    'EXC-' || LPAD(CAST(ROW_NUMBER() OVER (ORDER BY event_date, reference_id) AS VARCHAR), 3, '0') AS exception_id,
    *
FROM raw_exceptions
ORDER BY
    CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,
    event_date DESC;


-- ----------------------------------------------------------------------------
-- 2. MONTHLY ACCRUAL P&L (Ex-VAT, EUR; Costs stored as negative for Waterfall)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_monthly_pnl AS
WITH months AS (
    SELECT DISTINCT issue_month AS month FROM stg_erp_invoices
),
erp_rev AS (
    SELECT
        issue_month AS month,
        CAST(SUM(CASE WHEN status = 'ISSUED' THEN subtotal_eur ELSE 0 END) AS DECIMAL(18, 2)) AS gross_revenue_eur,
        CAST(SUM(CASE WHEN status = 'CREDIT_NOTE' THEN subtotal_eur ELSE 0 END) AS DECIMAL(18, 2)) AS refunds_eur,
        CAST(SUM(subtotal_eur) AS DECIMAL(18, 2)) AS net_revenue_eur
    FROM stg_erp_invoices
    WHERE NOT is_duplicate_erp
    GROUP BY issue_month
),
stripe_fees AS (
    SELECT
        created_month AS month,
        CAST(-SUM(fee_eur) AS DECIMAL(18, 2)) AS stripe_fees_eur
    FROM stg_stripe_txns
    GROUP BY created_month
),
fx_spreads AS (
    SELECT
        STRFTIME(booking_date, '%Y-%m') AS month,
        CAST(-SUM(fx_spread_eur) AS DECIMAL(18, 2)) AS fx_spread_eur
    FROM rec_layer2b_b2b_wires
    WHERE recon_status = 'MATCHED_WITH_FX_SPREAD'
    GROUP BY STRFTIME(booking_date, '%Y-%m')
),
bank_opex AS (
    SELECT
        booking_month AS month,
        CAST(SUM(CASE WHEN category_hint = 'COGS_SUPPLIER' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS cogs_eur,
        CAST(SUM(CASE WHEN category_hint = 'MARKETING_ADS' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS marketing_ads_eur,
        CAST(SUM(CASE WHEN category_hint IN ('SAAS_OPS', 'PAYROLL') THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS opex_fixed_eur,
        CAST(SUM(CASE WHEN category_hint = 'UNKNOWN' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS unclassified_outflows_eur
    FROM stg_bank_txns
    WHERE direction = 'OUT'
      AND category_hint <> 'TAX_VAT'  -- VAT settlement is a balance sheet liability, never P&L expense
    GROUP BY booking_month
)
SELECT
    m.month,
    COALESCE(r.gross_revenue_eur, 0.00) AS gross_revenue_eur,
    COALESCE(r.refunds_eur, 0.00) AS refunds_eur,
    COALESCE(r.net_revenue_eur, 0.00) AS net_revenue_eur,
    COALESCE(o.cogs_eur, 0.00) AS cogs_eur,
    CAST(COALESCE(r.net_revenue_eur, 0.00) + COALESCE(o.cogs_eur, 0.00) AS DECIMAL(18, 2)) AS gross_profit_eur,
    COALESCE(sf.stripe_fees_eur, 0.00) AS stripe_fees_eur,
    COALESCE(fx.fx_spread_eur, 0.00) AS fx_spread_eur,
    CAST(COALESCE(sf.stripe_fees_eur, 0.00) + COALESCE(fx.fx_spread_eur, 0.00) AS DECIMAL(18, 2)) AS financial_fees_eur,
    COALESCE(o.marketing_ads_eur, 0.00) AS marketing_ads_eur,
    COALESCE(o.opex_fixed_eur, 0.00) AS opex_fixed_eur,
    COALESCE(o.unclassified_outflows_eur, 0.00) AS unclassified_outflows_eur,
    CAST(
        COALESCE(r.net_revenue_eur, 0.00)
        + COALESCE(o.cogs_eur, 0.00)
        + COALESCE(sf.stripe_fees_eur, 0.00)
        + COALESCE(fx.fx_spread_eur, 0.00)
        + COALESCE(o.marketing_ads_eur, 0.00)
        + COALESCE(o.opex_fixed_eur, 0.00)
        + COALESCE(o.unclassified_outflows_eur, 0.00)
        AS DECIMAL(18, 2)
    ) AS ebitda_eur,
    ROUND(
        100.0 * (
            COALESCE(r.net_revenue_eur, 0.00)
            + COALESCE(o.cogs_eur, 0.00)
            + COALESCE(sf.stripe_fees_eur, 0.00)
            + COALESCE(fx.fx_spread_eur, 0.00)
            + COALESCE(o.marketing_ads_eur, 0.00)
            + COALESCE(o.opex_fixed_eur, 0.00)
            + COALESCE(o.unclassified_outflows_eur, 0.00)
        ) / NULLIF(r.net_revenue_eur, 0),
        1
    ) AS ebitda_margin_pct
FROM months m
LEFT JOIN erp_rev r ON m.month = r.month
LEFT JOIN stripe_fees sf ON m.month = sf.month
LEFT JOIN fx_spreads fx ON m.month = fx.month
LEFT JOIN bank_opex o ON m.month = o.month
ORDER BY m.month;


-- ----------------------------------------------------------------------------
-- 3. MONTHLY CONSOLIDATED CASH FLOW & RUNWAY (Base vs -30% Stress Scenario)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_monthly_cashflow AS
WITH monthly_bank AS (
    SELECT
        booking_month AS month,
        CAST(SUM(CASE WHEN direction = 'IN' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS cash_inflows_eur,
        CAST(SUM(CASE WHEN direction = 'OUT' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS cash_outflows_eur,
        CAST(SUM(CASE WHEN category_hint = 'TAX_VAT' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS vat_paid_eur,
        CAST(SUM(amount_eur) AS DECIMAL(18, 2)) AS net_cashflow_eur,
        CAST(SUM(CASE WHEN currency = 'EUR' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS net_flow_eur_acct,
        CAST(SUM(CASE WHEN currency = 'USD' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS net_flow_usd_acct,
        CAST(SUM(CASE WHEN currency = 'GBP' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) AS net_flow_gbp_acct
    FROM stg_bank_txns
    GROUP BY booking_month
),
running AS (
    SELECT
        month,
        cash_inflows_eur,
        cash_outflows_eur,
        vat_paid_eur,
        net_cashflow_eur,
        CAST(
            {{INITIAL_CASH_EUR}}
            + SUM(net_cashflow_eur) OVER (ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
            - net_cashflow_eur
            AS DECIMAL(18, 2)
        ) AS opening_cash_eur,
        CAST(
            {{INITIAL_CASH_EUR}}
            + SUM(net_cashflow_eur) OVER (ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
            AS DECIMAL(18, 2)
        ) AS ending_cash_eur,
        -- Trailing 3-month averages smooth out quarterly VAT and bulk inventory spikes
        AVG(cash_inflows_eur) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS avg_3m_inflows_eur,
        AVG(ABS(cash_outflows_eur)) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS avg_3m_outflows_eur,
        AVG(net_cashflow_eur) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS avg_3m_net_cf_eur,
        net_flow_eur_acct,
        net_flow_usd_acct,
        net_flow_gbp_acct
    FROM monthly_bank
)
SELECT
    month,
    opening_cash_eur,
    cash_inflows_eur,
    cash_outflows_eur,
    vat_paid_eur,
    net_cashflow_eur,
    ending_cash_eur,
    CAST(ROUND(avg_3m_net_cf_eur, 2) AS DECIMAL(18, 2)) AS avg_3m_net_cf_eur,
    -- Base Runway: capped at 18 months when cash-flow positive to keep chart scale clean
    CASE
        WHEN avg_3m_net_cf_eur >= 0 THEN 18.0
        ELSE LEAST(18.0, ROUND(ending_cash_eur / ABS(avg_3m_net_cf_eur), 1))
    END AS runway_months_base,
    -- Stress Runway: simulates an immediate -30% revenue shock against current burn
    CASE
        WHEN (avg_3m_inflows_eur * 0.70 - avg_3m_outflows_eur) >= 0 THEN 18.0
        ELSE LEAST(18.0, ROUND(ending_cash_eur / ABS(avg_3m_inflows_eur * 0.70 - avg_3m_outflows_eur), 1))
    END AS runway_months_stress,
    net_flow_eur_acct,
    net_flow_usd_acct,
    net_flow_gbp_acct
FROM running
ORDER BY month;


-- ----------------------------------------------------------------------------
-- 4. EXECUTIVE KPI SUMMARY (Single-row snapshot for dashboard cards)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_kpi_summary AS
WITH recon_counts AS (
    SELECT
        COUNT(*) AS total_items,
        SUM(CASE WHEN is_clean THEN 1 ELSE 0 END) AS auto_reconciled_items
    FROM (
        SELECT recon_status = 'MATCHED' AS is_clean FROM rec_layer1_erp_stripe
        UNION ALL
        SELECT recon_status IN ('MATCHED', 'IN_TRANSIT') AS is_clean FROM rec_layer2a_stripe_payouts
        UNION ALL
        SELECT recon_status IN ('MATCHED_REF_EXACT', 'MATCHED_WITH_FX_SPREAD', 'MATCHED_AMOUNT_WINDOW', 'OPEN_NOT_DUE') AS is_clean
        FROM rec_layer2b_b2b_wires
    )
),
latest_cash AS (
    SELECT ending_cash_eur, runway_months_base, runway_months_stress
    FROM mart_monthly_cashflow
    ORDER BY month DESC
    LIMIT 1
)
SELECT
    (SELECT CAST(SUM(net_revenue_eur) AS DECIMAL(18, 2)) FROM mart_monthly_pnl) AS total_net_revenue_eur,
    (SELECT CAST(SUM(ebitda_eur) AS DECIMAL(18, 2)) FROM mart_monthly_pnl) AS total_ebitda_eur,
    (SELECT CAST(ABS(SUM(financial_fees_eur)) AS DECIMAL(18, 2)) FROM mart_monthly_pnl) AS total_fees_and_fx_eur,
    (SELECT CAST(SUM(amount_eur) AS DECIMAL(18, 2)) FROM mart_exceptions) AS total_discrepancies_eur,
    (SELECT CAST(SUM(CASE WHEN impact_bucket = 'Cash Impact' THEN amount_eur ELSE 0 END) AS DECIMAL(18, 2)) FROM mart_exceptions) AS cash_impact_discrepancies_eur,
    (SELECT COUNT(*) FROM mart_exceptions) AS total_exceptions_count,
    ROUND(100.0 * rc.auto_reconciled_items / NULLIF(rc.total_items, 0), 1) AS auto_recon_rate_pct,
    lc.ending_cash_eur,
    lc.runway_months_base,
    lc.runway_months_stress
FROM recon_counts rc
CROSS JOIN latest_cash lc;