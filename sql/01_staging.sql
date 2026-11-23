-- ============================================================================
-- 01_staging.sql
-- Loads raw CSVs into typed DuckDB staging tables, normalizes multi-currency
-- amounts to EUR using daily ECB rates, cleans dirty references via Regex,
-- and flags duplicate ERP invoices without touching valid credit notes.
-- ============================================================================

CREATE OR REPLACE TABLE stg_fx_rates AS
SELECT
    CAST(rate_date AS DATE) AS rate_date,
    UPPER(TRIM(currency)) AS currency,
    CAST(eur_per_unit AS DECIMAL(12, 6)) AS eur_per_unit
FROM read_csv_auto('{{RAW_DIR}}/fx_rates_ecb.csv', header = true);


CREATE OR REPLACE TABLE stg_erp_invoices AS
WITH base AS (
    SELECT
        TRIM(erp_row_id) AS erp_row_id,
        TRIM(invoice_number) AS invoice_number,
        -- Strip spaces, slashes, hyphens for deterministic matching (e.g. "B2B 2025/0004" -> "B2B20250004")
        regexp_replace(UPPER(TRIM(invoice_number)), '[^A-Z0-9]', '', 'g') AS clean_inv_ref,
        CAST(issue_date AS DATE) AS issue_date,
        CAST(due_date AS DATE) AS due_date,
        STRFTIME(CAST(issue_date AS DATE), '%Y-%m') AS issue_month,
        UPPER(TRIM(channel)) AS channel,
        TRIM(customer_name) AS customer_name,
        UPPER(TRIM(customer_country)) AS customer_country,
        UPPER(TRIM(currency)) AS currency,
        CAST(subtotal_amount AS DECIMAL(18, 2)) AS subtotal_amount,
        CAST(vat_rate AS DECIMAL(6, 4)) AS vat_rate,
        CAST(vat_amount AS DECIMAL(18, 2)) AS vat_amount,
        CAST(total_amount AS DECIMAL(18, 2)) AS total_amount,
        TRIM(external_ref) AS external_ref,
        UPPER(TRIM(status)) AS status
    FROM read_csv_auto('{{RAW_DIR}}/erp_invoices.csv', header = true)
),
dedup_flagged AS (
    SELECT
        b.*,
        -- Partition by (channel, external_ref, status) so CREDIT_NOTE rows sharing
        -- a Stripe charge_id with an ISSUED invoice are NOT flagged as duplicates.
        ROW_NUMBER() OVER (
            PARTITION BY b.channel, b.external_ref, b.status
            ORDER BY b.erp_row_id
        ) > 1 AS is_duplicate_erp
    FROM base b
)
SELECT
    d.*,
    fx.eur_per_unit AS fx_rate_issue,
    CAST(ROUND(d.subtotal_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS subtotal_eur,
    CAST(ROUND(d.vat_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS vat_eur,
    CAST(ROUND(d.total_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS total_eur
FROM dedup_flagged d
JOIN stg_fx_rates fx
    ON d.issue_date = fx.rate_date
   AND d.currency = fx.currency;


CREATE OR REPLACE TABLE stg_stripe_txns AS
WITH base AS (
    SELECT
        TRIM(txn_id) AS txn_id,
        TRIM(charge_id) AS charge_id,
        NULLIF(TRIM(CAST(payout_id AS VARCHAR)), '') AS payout_id,
        CAST(created_at AS DATE) AS created_at,
        CAST(available_on AS DATE) AS available_on,
        STRFTIME(CAST(created_at AS DATE), '%Y-%m') AS created_month,
        LOWER(TRIM(type)) AS type,
        UPPER(TRIM(currency)) AS currency,
        CAST(gross_amount AS DECIMAL(18, 2)) AS gross_amount,
        CAST(fee_amount AS DECIMAL(18, 2)) AS fee_amount,
        CAST(net_amount AS DECIMAL(18, 2)) AS net_amount,
        TRIM(CAST(invoice_ref AS VARCHAR)) AS invoice_ref,
        regexp_replace(UPPER(TRIM(CAST(invoice_ref AS VARCHAR))), '[^A-Z0-9]', '', 'g') AS clean_invoice_ref,
        UPPER(TRIM(customer_country)) AS customer_country
    FROM read_csv_auto('{{RAW_DIR}}/stripe_balance_txns.csv', header = true)
)
SELECT
    b.*,
    fx.eur_per_unit AS fx_rate_txn,
    CAST(ROUND(b.gross_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS gross_eur,
    CAST(ROUND(b.fee_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS fee_eur,
    CAST(ROUND(b.net_amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS net_eur
FROM base b
JOIN stg_fx_rates fx
    ON b.created_at = fx.rate_date
   AND b.currency = fx.currency;


CREATE OR REPLACE TABLE stg_bank_txns AS
WITH base AS (
    SELECT
        TRIM(bank_txn_id) AS bank_txn_id,
        TRIM(account_name) AS account_name,
        CAST(booking_date AS DATE) AS booking_date,
        CAST(value_date AS DATE) AS value_date,
        STRFTIME(CAST(booking_date AS DATE), '%Y-%m') AS booking_month,
        UPPER(TRIM(direction)) AS direction,
        UPPER(TRIM(category_hint)) AS category_hint,
        UPPER(TRIM(currency)) AS currency,
        CAST(amount AS DECIMAL(18, 2)) AS amount,
        TRIM(description) AS description,
        -- Extract batched Stripe payout ID (e.g. "po_eur_20251003_00")
        NULLIF(LOWER(regexp_extract(UPPER(description), '(PO_[A-Z]+_[0-9]+_[0-9]+)', 1)), '') AS extracted_payout_id,
        -- Extract and normalize messy B2B invoice references from wire memos
        NULLIF(
            regexp_replace(
                regexp_extract(UPPER(description), '(B2B[\s\-/]*202[56][\s\-/]*[0-9]{4})', 1),
                '[^A-Z0-9]', '', 'g'
            ),
            ''
        ) AS extracted_b2b_ref
    FROM read_csv_auto('{{RAW_DIR}}/bank_revolut_wise.csv', header = true)
)
SELECT
    b.*,
    fx.eur_per_unit AS fx_rate_bank,
    CAST(ROUND(b.amount * fx.eur_per_unit, 2) AS DECIMAL(18, 2)) AS amount_eur
FROM base b
JOIN stg_fx_rates fx
    ON b.booking_date = fx.rate_date
   AND b.currency = fx.currency;