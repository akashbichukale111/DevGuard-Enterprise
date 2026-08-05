-- DevGuard V2 substrate — raw layer.
--
-- This is the table the reference-incident mutates. `user_id` is the column that gets
-- renamed to `customer_id` in step 1, and everything downstream — the dbt
-- model, the reporting query, the feature job, and ultimately the ML model —
-- breaks because of it. That breakage is REAL, not simulated.

CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE raw.users (
    user_id       BIGINT PRIMARY KEY,      -- <-- the reference-incident column
    email         TEXT        NOT NULL,
    country       TEXT        NOT NULL,
    signup_ts     TIMESTAMPTZ NOT NULL,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE  raw.users            IS 'Raw user records ingested from the application database.';
COMMENT ON COLUMN raw.users.user_id    IS 'Primary identifier for a user. Referenced by orders and by the ML feature build.';
COMMENT ON COLUMN raw.users.email      IS 'Contact email. Treated as PII.';
COMMENT ON COLUMN raw.users.country    IS 'ISO-3166 alpha-2 country code.';

CREATE TABLE raw.orders (
    order_id      BIGINT PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES raw.users(user_id),
    amount_cents  INTEGER     NOT NULL CHECK (amount_cents >= 0),
    status        TEXT        NOT NULL,
    ordered_at    TIMESTAMPTZ NOT NULL
);

COMMENT ON TABLE  raw.orders         IS 'Raw order events.';
COMMENT ON COLUMN raw.orders.user_id IS 'Foreign key to raw.users.user_id.';

-- Deterministic, generated data. Not "seeded demo data" in the no-fabrication rule sense —
-- these are real rows in a real database that real queries really read. What
-- would be dishonest is presenting datapack METADATA as if it were this.
INSERT INTO raw.users (user_id, email, country, signup_ts, is_active)
SELECT g,
       'user' || g || '@example.com',
       (ARRAY['US','GB','IN','DE','BR'])[1 + (g % 5)],
       TIMESTAMPTZ '2026-01-01 00:00:00+00' + (g || ' hours')::interval,
       (g % 7) <> 0
FROM generate_series(1, 2000) AS g;

INSERT INTO raw.orders (order_id, user_id, amount_cents, status, ordered_at)
SELECT g,
       1 + (g % 2000),
       ((g * 37) % 50000) + 100,
       (ARRAY['completed','completed','completed','refunded','cancelled'])[1 + (g % 5)],
       TIMESTAMPTZ '2026-02-01 00:00:00+00' + (g || ' minutes')::interval
FROM generate_series(1, 20000) AS g;

ANALYZE raw.users;
ANALYZE raw.orders;
