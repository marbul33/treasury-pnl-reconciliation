-- ============================================================================
-- 02_reconciliation.sql
-- Two-layer treasury reconciliation engine:
--   Layer 1  : D2C ERP Invoices <-> Stripe Charges (isolates missing/duplicate ERP)
--   Layer 2A : Batched Stripe Payouts <-> Bank Payout Inflows (isolates payout deltas & in-transit)
--   Layer 2B : B2B ERP Invoices <-> Bank Wires (Level-1 Regex + FX spread; Level-2 1:1 window)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- LAYER 1: D2C ERP Invoices <-> Stripe Charges
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE rec_layer1_erp_stripe AS
WITH clean_erp_d2c AS (
    SELECT *
    FROM stg_erp_invoices
    WHERE channel = 'D2C_STRIPE'
      AND status = 'ISSUED'
      AND NOT is_duplicate_erp
),
stripe_charges AS (
    SELECT *
    FROM stg_stripe_txns
    WHERE type = 'charge'
),
matched_and_missing AS (
    SELECT
        COALESCE(s.created_at, e.issue_date) AS event_date,
        COALESCE(s.created_month, e.issue_month) AS event_month,
        e.erp_row_id,
        e.invoice_number,
        s.txn_id AS stripe_txn_id,
        s.charge_id,
        COALESCE(s.currency, e.currency) AS currency,
        COALESCE(e.total_amount, 0.00) AS erp_total_orig,
        COALESCE(s.gross_amount, 0.00) AS stripe_gross_orig,
        COALESCE(s.gross_eur, e.total_eur) AS amount_eur,
        CASE
            WHEN e.erp_row_id IS NOT NULL AND s.txn_id IS NOT NULL THEN 'MATCHED'
            WHEN e.erp_row_id IS NULL AND s.txn_id IS NOT NULL THEN 'MISSING_ERP_INVOICE'
            ELSE 'MISSING_STRIPE_CHARGE'
        END AS recon_status
    FROM stripe_charges s
    FULL OUTER JOIN clean_erp_d2c e
        ON s.charge_id = e.external_ref
),
duplicate_erp_rows AS (
    SELECT
        issue_date AS event_date,
        issue_month AS event_month,
        erp_row_id,
        invoice_number,
        CAST(NULL AS VARCHAR) AS stripe_txn_id,
        external_ref AS charge_id,
        currency,
        total_amount AS erp_total_orig,
        CAST(0.00 AS DECIMAL(18, 2)) AS stripe_gross_orig,
        total_eur AS amount_eur,
        'DUPLICATE_ERP_INVOICE' AS recon_status
    FROM stg_erp_invoices
    WHERE is_duplicate_erp = TRUE
)
SELECT * FROM matched_and_missing
UNION ALL
SELECT * FROM duplicate_erp_rows;


-- ----------------------------------------------------------------------------
-- LAYER 2A: Stripe Batched Payouts <-> Bank Statement
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE rec_layer2a_stripe_payouts AS
WITH stripe_batches AS (
    SELECT
        payout_id,
        currency,
        COUNT(*) AS txn_count,
        MAX(available_on) AS expected_arrival_date,
        CAST(SUM(net_amount) AS DECIMAL(18, 2)) AS expected_net_orig,
        CAST(SUM(net_eur) AS DECIMAL(18, 2)) AS expected_net_eur
    FROM stg_stripe_txns
    WHERE payout_id IS NOT NULL
    GROUP BY payout_id, currency
),
bank_payouts AS (
    SELECT
        bank_txn_id,
        extracted_payout_id AS payout_id,
        booking_date,
        booking_month,
        currency,
        amount AS bank_amount_orig,
        amount_eur AS bank_amount_eur,
        fx_rate_bank
    FROM stg_bank_txns
    WHERE category_hint = 'STRIPE_PAYOUT'
)
SELECT
    COALESCE(b.booking_date, s.expected_arrival_date) AS event_date,
    STRFTIME(COALESCE(b.booking_date, s.expected_arrival_date), '%Y-%m') AS event_month,
    COALESCE(s.payout_id, b.payout_id) AS payout_id,
    b.bank_txn_id,
    COALESCE(s.currency, b.currency) AS currency,
    COALESCE(s.txn_count, 0) AS txn_count,
    COALESCE(s.expected_net_orig, 0.00) AS expected_net_orig,
    COALESCE(b.bank_amount_orig, 0.00) AS bank_amount_orig,
    CAST(COALESCE(s.expected_net_orig, 0.00) - COALESCE(b.bank_amount_orig, 0.00) AS DECIMAL(18, 2)) AS delta_orig,
    CAST(
        ROUND(
            (COALESCE(s.expected_net_orig, 0.00) - COALESCE(b.bank_amount_orig, 0.00)) * COALESCE(b.fx_rate_bank, 1.0),
            2
        ) AS DECIMAL(18, 2)
    ) AS delta_eur,
    CASE
        WHEN b.bank_txn_id IS NULL AND s.expected_arrival_date > DATE '{{END_DATE}}' THEN 'IN_TRANSIT'
        WHEN b.bank_txn_id IS NOT NULL AND s.payout_id IS NOT NULL
             AND ABS(s.expected_net_orig - b.bank_amount_orig) <= 0.01 THEN 'MATCHED'
        WHEN b.bank_txn_id IS NOT NULL AND s.payout_id IS NOT NULL THEN 'UNMATCHED_PAYOUT_DELTA'
        ELSE 'ORPHAN_BANK_PAYOUT'
    END AS recon_status
FROM stripe_batches s
FULL OUTER JOIN bank_payouts b
    ON s.payout_id = b.payout_id;


-- ----------------------------------------------------------------------------
-- LAYER 2B: B2B Invoices <-> Bank Wires (Hierarchical Level-1 & Level-2 1:1)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE rec_layer2b_b2b_wires AS
WITH b2b_erp AS (
    SELECT *
    FROM stg_erp_invoices
    WHERE channel = 'B2B_WIRE'
      AND status = 'ISSUED'
      AND NOT is_duplicate_erp
),
b2b_bank AS (
    SELECT *
    FROM stg_bank_txns
    WHERE category_hint = 'B2B_WIRE'
      AND direction = 'IN'
),
-- Level 1: Exact match on Regex-cleaned invoice reference (supports cross-currency FX spread)
lvl1_matches AS (
    SELECT
        e.erp_row_id,
        e.invoice_number,
        e.issue_date,
        e.due_date,
        e.customer_name,
        e.currency AS erp_currency,
        e.total_amount AS erp_total_orig,
        e.total_eur AS erp_total_eur,
        b.bank_txn_id,
        b.booking_date,
        b.currency AS bank_currency,
        b.amount AS bank_amount_orig,
        b.amount_eur AS bank_amount_eur,
        -- Expected EUR at payment-date ECB rate to isolate bank FX spread
        CAST(ROUND(e.total_amount * fx_pay.eur_per_unit, 2) AS DECIMAL(18, 2)) AS expected_eur_at_pay,
        CASE
            WHEN e.currency = b.currency THEN CAST(0.00 AS DECIMAL(18, 2))
            ELSE CAST(ROUND(ROUND(e.total_amount * fx_pay.eur_per_unit, 2) - b.amount_eur, 2) AS DECIMAL(18, 2))
        END AS fx_spread_eur
    FROM b2b_erp e
    JOIN b2b_bank b
        ON e.clean_inv_ref = b.extracted_b2b_ref
    JOIN stg_fx_rates fx_pay
        ON b.booking_date = fx_pay.rate_date
       AND e.currency = fx_pay.currency
),
lvl1_classified AS (
    SELECT
        erp_row_id,
        invoice_number,
        issue_date,
        due_date,
        customer_name,
        erp_currency,
        erp_total_orig,
        erp_total_eur,
        bank_txn_id,
        booking_date,
        bank_currency,
        bank_amount_orig,
        bank_amount_eur,
        fx_spread_eur,
        CASE
            WHEN erp_currency = bank_currency AND ABS(erp_total_orig - bank_amount_orig) <= 0.01
                THEN 'MATCHED_REF_EXACT'
            WHEN erp_currency <> bank_currency
                 AND ABS(fx_spread_eur) <= GREATEST(expected_eur_at_pay * {{FX_TOLERANCE_PCT}}, {{FX_TOLERANCE_ABS_EUR}})
                THEN 'MATCHED_WITH_FX_SPREAD'
            ELSE 'B2B_AMOUNT_MISMATCH'
        END AS recon_status
    FROM lvl1_matches
),
-- Level 2: Deterministic 1:1 pairing for wires missing an invoice reference
unmatched_erp_l1 AS (
    SELECT * FROM b2b_erp
    WHERE erp_row_id NOT IN (SELECT erp_row_id FROM lvl1_classified)
),
unmatched_bank_l1 AS (
    SELECT * FROM b2b_bank
    WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM lvl1_classified)
),
lvl2_candidates AS (
    SELECT
        e.erp_row_id,
        e.invoice_number,
        e.issue_date,
        e.due_date,
        e.customer_name,
        e.currency AS erp_currency,
        e.total_amount AS erp_total_orig,
        e.total_eur AS erp_total_eur,
        b.bank_txn_id,
        b.booking_date,
        b.currency AS bank_currency,
        b.amount AS bank_amount_orig,
        b.amount_eur AS bank_amount_eur,
        -- Rank both sides to strictly guarantee 1:1 pairing (zero Cartesian explosion)
        ROW_NUMBER() OVER (
            PARTITION BY e.erp_row_id
            ORDER BY
                CASE WHEN CONTAINS(UPPER(b.description), UPPER(e.customer_name)) THEN 0 ELSE 1 END,
                ABS(b.booking_date - e.issue_date),
                b.bank_txn_id
        ) AS rn_erp,
        ROW_NUMBER() OVER (
            PARTITION BY b.bank_txn_id
            ORDER BY
                CASE WHEN CONTAINS(UPPER(b.description), UPPER(e.customer_name)) THEN 0 ELSE 1 END,
                ABS(b.booking_date - e.issue_date),
                e.erp_row_id
        ) AS rn_bank
    FROM unmatched_erp_l1 e
    JOIN unmatched_bank_l1 b
        ON e.currency = b.currency
       AND ABS(e.total_amount - b.amount) <= 0.01
       AND b.booking_date BETWEEN e.issue_date AND (e.due_date + {{B2B_MATCH_WINDOW_DAYS}})
),
lvl2_matched AS (
    SELECT
        erp_row_id,
        invoice_number,
        issue_date,
        due_date,
        customer_name,
        erp_currency,
        erp_total_orig,
        erp_total_eur,
        bank_txn_id,
        booking_date,
        bank_currency,
        bank_amount_orig,
        bank_amount_eur,
        CAST(0.00 AS DECIMAL(18, 2)) AS fx_spread_eur,
        'MATCHED_AMOUNT_WINDOW' AS recon_status
    FROM lvl2_candidates
    WHERE rn_erp = 1 AND rn_bank = 1
),
all_matched AS (
    SELECT * FROM lvl1_classified
    UNION ALL
    SELECT * FROM lvl2_matched
),
-- Remaining unmatched B2B ERP invoices: split Overdue (anomaly) vs Open Not Due (normal AR)
remaining_erp AS (
    SELECT
        e.erp_row_id,
        e.invoice_number,
        e.issue_date,
        e.due_date,
        e.customer_name,
        e.currency AS erp_currency,
        e.total_amount AS erp_total_orig,
        e.total_eur AS erp_total_eur,
        CAST(NULL AS VARCHAR) AS bank_txn_id,
        CAST(NULL AS DATE) AS booking_date,
        CAST(NULL AS VARCHAR) AS bank_currency,
        CAST(0.00 AS DECIMAL(18, 2)) AS bank_amount_orig,
        CAST(0.00 AS DECIMAL(18, 2)) AS bank_amount_eur,
        CAST(0.00 AS DECIMAL(18, 2)) AS fx_spread_eur,
        CASE
            WHEN e.due_date < DATE '{{END_DATE}}' THEN 'OVERDUE_B2B_INVOICE'
            ELSE 'OPEN_NOT_DUE'
        END AS recon_status
    FROM b2b_erp e
    WHERE e.erp_row_id NOT IN (SELECT erp_row_id FROM all_matched)
)
SELECT * FROM all_matched
UNION ALL
SELECT * FROM remaining_erp;