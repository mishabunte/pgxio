-- Public DATA in the disposable benchmark fixture, not production authorization.
CREATE OR REPLACE FUNCTION web.bench_public_one(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT jsonb_build_object('id',r.id,'version',r.version,'payload',r.payload)
  FROM pgxbench.rows r JOIN pgxbench.accounts a USING(subject)
  WHERE a.enabled AND r.subject=input->>'tenant'
    AND r.id=(input->>'id')::integer
$$;
CREATE OR REPLACE FUNCTION web.bench_public_page(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
  SELECT jsonb_build_object('rows', coalesce(jsonb_agg(jsonb_build_object(
    'id',r.id,'version',r.version,'payload',r.payload) ORDER BY r.id),'[]'::jsonb))
  FROM (SELECT r.* FROM pgxbench.rows r JOIN pgxbench.accounts a USING(subject)
    WHERE a.enabled AND r.subject=input->>'tenant'
    ORDER BY r.id LIMIT 100) r
$$;
CREATE OR REPLACE FUNCTION web.bench_public_write(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
DECLARE result jsonb; routing text;
BEGIN
  SELECT route INTO STRICT routing FROM pgxbench.accounts
    WHERE enabled AND subject=input->>'tenant';
  UPDATE pgxbench.rows SET version=version+1
    WHERE subject=input->>'tenant' AND id=(input->>'id')::integer
    RETURNING jsonb_build_object('id',id,'version',version,'payload',payload) INTO STRICT result;
  PERFORM pg_notify('pgxio_events',routing);
  RETURN result;
END $$;
ALTER FUNCTION web.bench_public_one(jsonb) OWNER TO pgxbench_executor;
REVOKE ALL ON FUNCTION web.bench_public_one(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.bench_public_one(jsonb) TO web;
ALTER FUNCTION web.bench_public_page(jsonb) OWNER TO pgxbench_executor;
REVOKE ALL ON FUNCTION web.bench_public_page(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.bench_public_page(jsonb) TO web;
ALTER FUNCTION web.bench_public_write(jsonb) OWNER TO pgxbench_executor;
REVOKE ALL ON FUNCTION web.bench_public_write(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.bench_public_write(jsonb) TO web;
