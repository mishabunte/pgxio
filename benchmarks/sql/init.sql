-- This schema belongs ONLY to the isolated pgxbench database.
CREATE ROLE web LOGIN PASSWORD 'WEB_PASSWORD_PLACEHOLDER'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE pgxbench_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA web;
CREATE SCHEMA pgxbench;
REVOKE ALL ON SCHEMA web, pgxbench FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA web TO web;
GRANT USAGE ON SCHEMA pgxbench TO pgxbench_executor;
ALTER DEFAULT PRIVILEGES IN SCHEMA web REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE TABLE pgxbench.accounts(subject text PRIMARY KEY, route text UNIQUE NOT NULL, enabled boolean NOT NULL DEFAULT true);
CREATE TABLE pgxbench.rows(subject text REFERENCES pgxbench.accounts(subject), id integer,
  version bigint NOT NULL DEFAULT 1, payload text NOT NULL, PRIMARY KEY(subject,id));
GRANT SELECT ON pgxbench.accounts TO pgxbench_executor;
GRANT SELECT, UPDATE ON pgxbench.rows TO pgxbench_executor;

CREATE FUNCTION web.bench_one(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT jsonb_build_object('id',r.id,'version',r.version,'payload',r.payload)
  FROM pgxbench.rows r JOIN pgxbench.accounts a USING(subject)
  WHERE a.enabled AND r.subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'
    AND r.id=(input->>'id')::integer
$$;
CREATE FUNCTION web.bench_page(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT jsonb_build_object('rows', coalesce(jsonb_agg(jsonb_build_object(
    'id',r.id,'version',r.version,'payload',r.payload) ORDER BY r.id),'[]'::jsonb))
  FROM (SELECT r.* FROM pgxbench.rows r JOIN pgxbench.accounts a USING(subject)
    WHERE a.enabled AND r.subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'
    ORDER BY r.id LIMIT 100) r
$$;
CREATE FUNCTION web.bench_write(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
DECLARE result jsonb; routing text;
BEGIN
  SELECT route INTO STRICT routing FROM pgxbench.accounts
    WHERE enabled AND subject=current_setting('request.jwt.claims',true)::jsonb->>'sub';
  UPDATE pgxbench.rows SET version=version+1
    WHERE subject=current_setting('request.jwt.claims',true)::jsonb->>'sub' AND id=(input->>'id')::integer
    RETURNING jsonb_build_object('id',id,'version',version,'payload',payload) INTO STRICT result;
  PERFORM pg_notify('pgxio_events',routing);
  RETURN result;
END $$;
CREATE FUNCTION web.bench_authorize(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT coalesce((SELECT to_jsonb(route) FROM pgxbench.accounts
    WHERE enabled AND subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'),'null'::jsonb)
$$;
ALTER FUNCTION web.bench_one(jsonb) OWNER TO pgxbench_executor;
ALTER FUNCTION web.bench_page(jsonb) OWNER TO pgxbench_executor;
ALTER FUNCTION web.bench_write(jsonb) OWNER TO pgxbench_executor;
ALTER FUNCTION web.bench_authorize(jsonb) OWNER TO pgxbench_executor;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA web FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA web TO web;
