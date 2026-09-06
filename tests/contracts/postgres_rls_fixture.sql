\set ON_ERROR_STOP on
-- Synthetic disposable database only. Never a production migration.
CREATE ROLE fixture_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE SCHEMA offline_fixture;
CREATE TABLE offline_fixture.orders (
    tenant_id uuid NOT NULL,
    order_id text NOT NULL,
    status text NOT NULL,
    PRIMARY KEY (tenant_id, order_id)
);
CREATE TABLE offline_fixture.outbox (
    tenant_id uuid NOT NULL,
    event_id text NOT NULL,
    order_id text NOT NULL,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, order_id)
        REFERENCES offline_fixture.orders (tenant_id, order_id)
);
ALTER TABLE offline_fixture.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE offline_fixture.orders FORCE ROW LEVEL SECURITY;
ALTER TABLE offline_fixture.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE offline_fixture.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_orders ON offline_fixture.orders
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_outbox ON offline_fixture.outbox
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT USAGE ON SCHEMA offline_fixture TO fixture_app;
GRANT SELECT, INSERT, UPDATE ON offline_fixture.orders, offline_fixture.outbox TO fixture_app;
SET ROLE fixture_app;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'test role bypasses RLS';
    END IF;
END $$;
BEGIN;
SET LOCAL app.tenant_id = '00000000-0000-0000-0000-00000000000a';
INSERT INTO offline_fixture.orders VALUES ('00000000-0000-0000-0000-00000000000a', 'same-order', 'A');
INSERT INTO offline_fixture.outbox VALUES ('00000000-0000-0000-0000-00000000000a', 'same-event', 'same-order');
COMMIT;
BEGIN;
SET LOCAL app.tenant_id = '00000000-0000-0000-0000-00000000000b';
INSERT INTO offline_fixture.orders VALUES ('00000000-0000-0000-0000-00000000000b', 'same-order', 'B');
INSERT INTO offline_fixture.outbox VALUES ('00000000-0000-0000-0000-00000000000b', 'same-event', 'same-order');
COMMIT;
DO $$ BEGIN
    IF EXISTS (SELECT FROM offline_fixture.orders) OR EXISTS (SELECT FROM offline_fixture.outbox) THEN
        RAISE EXCEPTION 'missing tenant exposed rows';
    END IF;
    BEGIN
        INSERT INTO offline_fixture.orders VALUES ('00000000-0000-0000-0000-00000000000a', 'no-context', 'bad');
        RAISE EXCEPTION 'missing tenant insert accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;
BEGIN;
SET LOCAL app.tenant_id = '00000000-0000-0000-0000-00000000000a';
DO $$ DECLARE changed integer; BEGIN
    IF (SELECT count(*) FROM offline_fixture.orders) <> 1 OR
       (SELECT status FROM offline_fixture.orders WHERE order_id = 'same-order') <> 'A' OR
       (SELECT count(*) FROM offline_fixture.outbox) <> 1 THEN
        RAISE EXCEPTION 'tenant isolation failed';
    END IF;
    UPDATE offline_fixture.orders SET status = 'bad'
        WHERE tenant_id = '00000000-0000-0000-0000-00000000000b';
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'foreign tenant update accepted'; END IF;
    BEGIN
        INSERT INTO offline_fixture.orders VALUES ('00000000-0000-0000-0000-00000000000b', 'injected', 'bad');
        RAISE EXCEPTION 'foreign tenant insert accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        UPDATE offline_fixture.orders SET tenant_id = '00000000-0000-0000-0000-00000000000b';
        RAISE EXCEPTION 'tenant reassignment accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO offline_fixture.outbox VALUES ('00000000-0000-0000-0000-00000000000b', 'injected', 'same-order');
        RAISE EXCEPTION 'foreign outbox insert accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO offline_fixture.orders VALUES ('00000000-0000-0000-0000-00000000000a', 'rolled-back', 'A');
        INSERT INTO offline_fixture.outbox VALUES ('00000000-0000-0000-0000-00000000000a', 'rolled-back', 'rolled-back');
        RAISE EXCEPTION USING ERRCODE = 'Z0001', MESSAGE = 'synthetic transaction failure';
    EXCEPTION WHEN SQLSTATE 'Z0001' THEN NULL;
    END;
    IF EXISTS (SELECT FROM offline_fixture.orders WHERE order_id = 'rolled-back') OR
       EXISTS (SELECT FROM offline_fixture.outbox WHERE event_id = 'rolled-back') THEN
        RAISE EXCEPTION 'order outbox atomic rollback failed';
    END IF;
    IF has_table_privilege(current_user, 'offline_fixture.orders', 'TRUNCATE') THEN
        RAISE EXCEPTION 'unfiltered truncate permission granted';
    END IF;
END $$;
COMMIT;
DO $$ BEGIN
    IF NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL OR
       EXISTS (SELECT FROM offline_fixture.orders) THEN
        RAISE EXCEPTION 'SET LOCAL tenant leaked after transaction';
    END IF;
END $$;
SELECT version();
SELECT 'OFFLINE_RLS_AND_ATOMICITY_PASS' AS fixture_result;
