-- Logistics firm analytical warehouse (star schema, simplified)
-- Load with: psql "$DATABASE_URL" -f schema/logistics_dw.sql

CREATE SCHEMA IF NOT EXISTS dw;

SET search_path TO dw, public;

-- === Dimensions ===

CREATE TABLE IF NOT EXISTS dim_date (
    date_key        INTEGER PRIMARY KEY,  -- YYYYMMDD
    full_date       DATE NOT NULL UNIQUE,
    year            SMALLINT NOT NULL,
    quarter         SMALLINT NOT NULL,
    month           SMALLINT NOT NULL,
    week_of_year    SMALLINT NOT NULL,
    day_of_month    SMALLINT NOT NULL,
    day_of_week     SMALLINT NOT NULL,
    is_weekend      BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_customer (
    customer_sk     BIGSERIAL PRIMARY KEY,
    customer_id     VARCHAR(64) NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    segment         VARCHAR(32) NOT NULL,  -- retail, smb, enterprise
    country_code    CHAR(2) NOT NULL,
    city            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_location (
    location_sk     BIGSERIAL PRIMARY KEY,
    site_code       VARCHAR(32) NOT NULL UNIQUE,
    site_name       TEXT NOT NULL,
    location_type   VARCHAR(16) NOT NULL,  -- hub, warehouse, port, airport
    country_code    CHAR(2) NOT NULL,
    region          TEXT NOT NULL,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dim_carrier (
    carrier_sk      BIGSERIAL PRIMARY KEY,
    carrier_code    VARCHAR(16) NOT NULL UNIQUE,
    carrier_name    TEXT NOT NULL,
    mode            VARCHAR(16) NOT NULL  -- road, rail, air, ocean
);

CREATE TABLE IF NOT EXISTS dim_service (
    service_sk      BIGSERIAL PRIMARY KEY,
    service_code    VARCHAR(32) NOT NULL UNIQUE,
    service_name    TEXT NOT NULL,
    sla_hours       INTEGER
);

-- === Facts ===

CREATE TABLE IF NOT EXISTS fact_shipment (
    shipment_sk         BIGSERIAL PRIMARY KEY,
    shipment_id         VARCHAR(64) NOT NULL UNIQUE,
    customer_sk         BIGINT NOT NULL REFERENCES dim_customer(customer_sk),
    origin_sk           BIGINT NOT NULL REFERENCES dim_location(location_sk),
    dest_sk             BIGINT NOT NULL REFERENCES dim_location(location_sk),
    carrier_sk          BIGINT NOT NULL REFERENCES dim_carrier(carrier_sk),
    service_sk          BIGINT NOT NULL REFERENCES dim_service(service_sk),
    booked_date_key     INTEGER NOT NULL REFERENCES dim_date(date_key),
    ship_date_key       INTEGER REFERENCES dim_date(date_key),
    weight_kg           NUMERIC(14, 3) NOT NULL,
    volume_m3           NUMERIC(14, 4),
    freight_charge_usd  NUMERIC(14, 2),
    currency            CHAR(3) NOT NULL DEFAULT 'USD',
    status              VARCHAR(16) NOT NULL  -- booked, in_transit, delivered, exception
);

CREATE TABLE IF NOT EXISTS fact_delivery_event (
    event_sk            BIGSERIAL PRIMARY KEY,
    shipment_sk         BIGINT NOT NULL REFERENCES fact_shipment(shipment_sk),
    event_date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    event_type          VARCHAR(32) NOT NULL,  -- pickup, hub_scan, customs, delivery_attempt, pod
    location_sk         BIGINT REFERENCES dim_location(location_sk),
    delay_minutes       INTEGER
);

CREATE TABLE IF NOT EXISTS fact_inventory_snapshot (
    snapshot_sk         BIGSERIAL PRIMARY KEY,
    snapshot_date_key   INTEGER NOT NULL REFERENCES dim_date(date_key),
    location_sk         BIGINT NOT NULL REFERENCES dim_location(location_sk),
    sku                 VARCHAR(64) NOT NULL,
    quantity_on_hand    INTEGER NOT NULL,
    quantity_reserved   INTEGER NOT NULL DEFAULT 0
);

-- Helpful indexes for analytics
CREATE INDEX IF NOT EXISTS idx_shipment_booked ON fact_shipment (booked_date_key);
CREATE INDEX IF NOT EXISTS idx_shipment_customer ON fact_shipment (customer_sk);
CREATE INDEX IF NOT EXISTS idx_delivery_shipment ON fact_delivery_event (shipment_sk);
CREATE INDEX IF NOT EXISTS idx_inv_loc_date ON fact_inventory_snapshot (location_sk, snapshot_date_key);

-- === Seed sample rows (deterministic small set) ===

INSERT INTO dim_date (date_key, full_date, year, quarter, month, week_of_year, day_of_month, day_of_week, is_weekend)
VALUES
    (20260101, '2026-01-01', 2026, 1, 1, 1, 1, 4, FALSE),
    (20260115, '2026-01-15', 2026, 1, 1, 3, 15, 4, FALSE),
    (20260201, '2026-02-01', 2026, 1, 2, 5, 1, 7, TRUE)
ON CONFLICT (date_key) DO NOTHING;

INSERT INTO dim_customer (customer_id, name, segment, country_code, city)
VALUES
    ('CUST-1001', 'Acme Retail EU', 'retail', 'DE', 'Berlin'),
    ('CUST-2002', 'Globex Corp', 'enterprise', 'US', 'Chicago')
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO dim_location (site_code, site_name, location_type, country_code, region, latitude, longitude)
VALUES
    ('HUB-DE-BER', 'Berlin Sort Hub', 'hub', 'DE', 'EMEA', 52.52, 13.405),
    ('WH-US-ORD', 'Chicago Warehouse', 'warehouse', 'US', 'AMER', 41.878, -87.630),
    ('PORT-NL-RTM', 'Rotterdam Port', 'port', 'NL', 'EMEA', 51.92, 4.48)
ON CONFLICT (site_code) DO NOTHING;

INSERT INTO dim_carrier (carrier_code, carrier_name, mode)
VALUES
    ('FDX', 'FastFreight Express', 'air'),
    ('RL', 'RailLink', 'rail')
ON CONFLICT (carrier_code) DO NOTHING;

INSERT INTO dim_service (service_code, service_name, sla_hours)
VALUES
    ('STD', 'Standard', 120),
    ('EXP', 'Express', 48)
ON CONFLICT (service_code) DO NOTHING;

INSERT INTO fact_shipment (
    shipment_id, customer_sk, origin_sk, dest_sk, carrier_sk, service_sk,
    booked_date_key, ship_date_key, weight_kg, volume_m3, freight_charge_usd, status
)
SELECT
    'SHP-9001',
    c.customer_sk, o.location_sk, d.location_sk, car.carrier_sk, s.service_sk,
    20260115, 20260115, 1250.5, 12.3, 4500.00, 'delivered'
FROM dim_customer c
JOIN dim_location o ON o.site_code = 'HUB-DE-BER'
JOIN dim_location d ON d.site_code = 'WH-US-ORD'
JOIN dim_carrier car ON car.carrier_code = 'FDX'
JOIN dim_service s ON s.service_code = 'EXP'
WHERE c.customer_id = 'CUST-1001'
ON CONFLICT (shipment_id) DO NOTHING;

INSERT INTO fact_delivery_event (shipment_sk, event_date_key, event_type, location_sk, delay_minutes)
SELECT fs.shipment_sk, 20260115, 'pickup', o.location_sk, 0
FROM fact_shipment fs
JOIN dim_location o ON o.site_code = 'HUB-DE-BER'
WHERE fs.shipment_id = 'SHP-9001'
  AND NOT EXISTS (
      SELECT 1 FROM fact_delivery_event e
      WHERE e.shipment_sk = fs.shipment_sk AND e.event_type = 'pickup'
  );
