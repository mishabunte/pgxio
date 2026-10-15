-- Test-only endpoint. Never install in a production contract/database.
CREATE FUNCTION web.perf_probe(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxio_private AS $$
DECLARE statement_name text;
BEGIN
  IF input ? 'marker' THEN
    INSERT INTO pgxio_private.tasks(subject,title) VALUES
      (current_setting('request.jwt.claims')::jsonb->>'sub',input->>'marker');
  END IF;
  IF input ? 'delay' THEN PERFORM pg_sleep((input->>'delay')::double precision); END IF;
  IF input->>'mode'='error' THEN RAISE EXCEPTION 'test error'; END IF;
  IF input->>'mode'='invalid' THEN RETURN '{}'::jsonb; END IF;
  IF input->>'mode'='invalidate' THEN
    SELECT name INTO statement_name FROM pg_prepared_statements
      WHERE name LIKE 'pgxio_%' AND statement LIKE '%set_config(%' LIMIT 1;
    IF statement_name IS NOT NULL THEN EXECUTE format('DEALLOCATE %I',statement_name); END IF;
  END IF;
  RETURN jsonb_build_object('pid',pg_backend_pid(),
      'subject',current_setting('request.jwt.claims')::jsonb->>'sub',
      'readonly',current_setting('transaction_read_only'),
      'prepared',(SELECT count(*) FROM pg_prepared_statements WHERE name LIKE 'pgxio_%'),
      'timeout',current_setting('statement_timeout'),'lock_timeout',current_setting('lock_timeout'));
END $$;
ALTER FUNCTION web.perf_probe(jsonb) OWNER TO pgxio_executor;
REVOKE ALL ON FUNCTION web.perf_probe(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.perf_probe(jsonb) TO web;
