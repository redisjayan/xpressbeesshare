-- Bulk synthetic data for dw (PostgreSQL)
-- Load after schema: psql "$DATABASE_URL" -f schema/logistics_dw.sql && psql "$DATABASE_URL" -f schema/insert_data.sql
--
-- Produces: dim_date (~731 days), 10_000 customers, 100 locations, 100 carriers, 50 services,
--           fact_shipment (100_000 rows), fact_delivery_event (300_000 rows), fact_inventory_snapshot (5_000 rows).
--
-- Truncates existing dw fact and dimension data so row counts match this script (removes seed rows from logistics_dw.sql).

SET search_path TO dw, public;

BEGIN;

-- fact_delivery_event references fact_shipment; CASCADE clears dependent rows in one statement.
TRUNCATE TABLE fact_shipment RESTART IDENTITY CASCADE;
TRUNCATE TABLE fact_inventory_snapshot RESTART IDENTITY;
TRUNCATE TABLE dim_customer, dim_location, dim_carrier, dim_service, dim_date RESTART IDENTITY CASCADE;

-- === dim_date: calendar 2024-01-01 through 2025-12-31 ===

INSERT INTO dim_date (date_key, full_date, year, quarter, month, week_of_year, day_of_month, day_of_week, is_weekend)
SELECT
    to_char(d, 'YYYYMMDD')::integer,
    d::date,
    EXTRACT(YEAR FROM d)::smallint,
    EXTRACT(QUARTER FROM d)::smallint,
    EXTRACT(MONTH FROM d)::smallint,
    EXTRACT(WEEK FROM d)::smallint,
    EXTRACT(DAY FROM d)::smallint,
    EXTRACT(ISODOW FROM d)::smallint,
    EXTRACT(ISODOW FROM d) IN (6, 7)
FROM generate_series('2024-01-01'::date, '2025-12-31'::date, interval '1 day') AS d;

-- === Dimensions ===

INSERT INTO dim_customer (customer_id, name, segment, country_code, city)
SELECT
    'CUST' || lpad(gs::text, 6, '0'),
    'Customer ' || gs::text,
    (ARRAY['retail', 'smb', 'enterprise'])[1 + ((gs - 1) % 3)],
    (ARRAY['US', 'DE', 'NL', 'GB', 'FR', 'CA', 'MX', 'BR', 'IN', 'CN'])[1 + ((gs - 1) % 10)],
    'City ' || gs::text
FROM generate_series(1, 10000) AS gs;

INSERT INTO dim_location (site_code, site_name, location_type, country_code, region, latitude, longitude)
SELECT
    'LOC-' || lpad(gs::text, 5, '0'),
    'Logistics Site ' || gs::text,
    (ARRAY['hub', 'warehouse', 'port', 'airport'])[1 + ((gs - 1) % 4)],
    (ARRAY['US', 'DE', 'NL', 'GB', 'FR', 'CA', 'SG', 'AE', 'JP', 'AU'])[1 + ((gs - 1) % 10)],
    (ARRAY['AMER', 'EMEA', 'APAC'])[1 + ((gs - 1) % 3)],
    35.0 + (gs % 15) + random(),
    -120.0 + (gs % 60) + random()
FROM generate_series(1, 100) AS gs;

INSERT INTO dim_carrier (carrier_code, carrier_name, mode)
SELECT
    'CAR-' || lpad(gs::text, 4, '0'),
    'Carrier ' || gs::text,
    (ARRAY['road', 'rail', 'air', 'ocean'])[1 + ((gs - 1) % 4)]
FROM generate_series(1, 100) AS gs;

INSERT INTO dim_service (service_code, service_name, sla_hours)
SELECT
    'SVC-' || lpad(gs::text, 3, '0'),
    'Service Offering ' || gs::text,
    24 * (1 + ((gs - 1) % 14))
FROM generate_series(1, 50) AS gs;

-- === fact_shipment: 100k rows referencing all dimensions ===

INSERT INTO fact_shipment (
    shipment_id,
    customer_sk,
    origin_sk,
    dest_sk,
    carrier_sk,
    service_sk,
    booked_date_key,
    ship_date_key,
    weight_kg,
    volume_m3,
    freight_charge_usd,
    currency,
    status
)
WITH
    cust AS (SELECT array_agg(customer_sk ORDER BY customer_sk) AS sk FROM dim_customer),
    loc AS (SELECT array_agg(location_sk ORDER BY location_sk) AS sk FROM dim_location),
    carr AS (SELECT array_agg(carrier_sk ORDER BY carrier_sk) AS sk FROM dim_carrier),
    svc AS (SELECT array_agg(service_sk ORDER BY service_sk) AS sk FROM dim_service),
    dk AS (SELECT array_agg(date_key ORDER BY date_key) AS d FROM dim_date)
SELECT
    'SHP-' || lpad(g::text, 10, '0'),
    cust.sk[1 + (mod(g::bigint * 7919, cardinality(cust.sk)::bigint))::integer],
    loc.sk[1 + (mod(g::bigint * 5003, cardinality(loc.sk)::bigint))::integer],
    loc.sk[1 + (mod(g::bigint * 7001 + 17, cardinality(loc.sk)::bigint))::integer],
    carr.sk[1 + (mod(g::bigint * 30011, cardinality(carr.sk)::bigint))::integer],
    svc.sk[1 + (mod(g::bigint * 9001, cardinality(svc.sk)::bigint))::integer],
    dk.d[1 + (mod(g::bigint * 11, cardinality(dk.d)::bigint))::integer],
    dk.d[1 + (mod(g::bigint * 11 + 1 + (g % 7), cardinality(dk.d)::bigint))::integer],
    round((random() * 4990 + 10)::numeric, 3),
    round((random() * 79 + 0.05)::numeric, 4),
    round((random() * 19950 + 50)::numeric, 2),
    'USD',
    (ARRAY['booked', 'in_transit', 'delivered', 'exception'])[1 + ((g - 1) % 4)]
FROM generate_series(1, 100000) AS g
CROSS JOIN cust
CROSS JOIN loc
CROSS JOIN carr
CROSS JOIN svc
CROSS JOIN dk;

-- === fact_delivery_event: three events per shipment ===

INSERT INTO fact_delivery_event (shipment_sk, event_date_key, event_type, location_sk, delay_minutes)
WITH
    loc AS (SELECT array_agg(location_sk ORDER BY location_sk) AS sk FROM dim_location),
    dk AS (SELECT array_agg(date_key ORDER BY date_key) AS d FROM dim_date)
SELECT
    fs.shipment_sk,
    dk.d[1 + (fs.shipment_sk % cardinality(dk.d))],
    e.event_type,
    loc.sk[1 + ((fs.shipment_sk * 7 + e.ord) % cardinality(loc.sk))],
    (random() * 200)::integer
FROM fact_shipment fs
CROSS JOIN LATERAL (
    VALUES
        (1, 'pickup'),
        (2, 'hub_scan'),
        (3, 'pod')
) AS e(ord, event_type)
CROSS JOIN loc
CROSS JOIN dk;

-- === fact_inventory_snapshot: 100 locations x 50 snapshot dates ===

INSERT INTO fact_inventory_snapshot (snapshot_date_key, location_sk, sku, quantity_on_hand, quantity_reserved)
WITH
    loc AS (SELECT array_agg(location_sk ORDER BY location_sk) AS sk FROM dim_location),
    dk AS (SELECT array_agg(date_key ORDER BY date_key) AS d FROM dim_date)
SELECT
    dk.d[1 + ((li + si) * 13) % cardinality(dk.d)],
    loc.sk[li],
    'SKU-' || lpad((li * 1000 + si)::text, 8, '0'),
    (100 + (random() * 9900)::integer),
    (random() * 400)::integer
FROM generate_series(1, 100) AS li
CROSS JOIN generate_series(1, 50) AS si
CROSS JOIN loc
CROSS JOIN dk;

COMMIT;
