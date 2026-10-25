-- Run AFTER the Realtime service has migrated its tenant database.
-- The official Supabase PostgreSQL image supplies auth/realtime dependencies.
GRANT USAGE ON SCHEMA realtime TO web;
GRANT SELECT ON realtime.messages TO web;
CREATE OR REPLACE FUNCTION web.bench_topic_allowed(topic text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT EXISTS(SELECT FROM pgxbench.accounts WHERE enabled
    AND subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'
    AND 'bench:'||route=topic)
$$;
ALTER FUNCTION web.bench_topic_allowed(text) OWNER TO pgxbench_executor;
REVOKE ALL ON FUNCTION web.bench_topic_allowed(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.bench_topic_allowed(text) TO web;
DROP POLICY IF EXISTS pgxbench_receive ON realtime.messages;
CREATE POLICY pgxbench_receive ON realtime.messages FOR SELECT TO web
  USING (extension='broadcast' AND web.bench_topic_allowed((SELECT realtime.topic())));
CREATE OR REPLACE FUNCTION pgxbench.broadcast_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxbench,realtime AS $$
DECLARE routing text;
BEGIN
  SELECT route INTO STRICT routing FROM pgxbench.accounts WHERE subject=NEW.subject;
  PERFORM realtime.send(jsonb_build_object('type','changed'),'changed','bench:'||routing,true);
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION pgxbench.broadcast_change() FROM PUBLIC;
DROP TRIGGER IF EXISTS pgxbench_broadcast ON pgxbench.rows;
CREATE TRIGGER pgxbench_broadcast AFTER UPDATE ON pgxbench.rows
  FOR EACH ROW EXECUTE FUNCTION pgxbench.broadcast_change();
