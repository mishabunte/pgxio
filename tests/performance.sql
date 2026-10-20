-- Test-only endpoint. Never install in a production contract/database.
CREATE FUNCTION web.perf_probe(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxio_private AS $$
DECLARE statement_name text;
BEGIN
  IF input ? 'marker' THEN
    INSERT INTO pgxio_private.tasks(subject,title) VALUES
      (current_setting('request.jwt.claims')::jsonb->>'sub',input->>'marker');
  END IF;
  IF input ? 'gate' THEN
    PERFORM set_config('lock_timeout','0',true);
    PERFORM pg_advisory_xact_lock((input->>'gate')::bigint);
  END IF;
  IF input ? 'delay' THEN PERFORM pg_sleep((input->>'delay')::double precision); END IF;
  IF input->>'mode'='error' THEN RAISE EXCEPTION 'test error'; END IF;
  IF input->>'mode'='input_error' THEN RAISE EXCEPTION USING ERRCODE='PT400', MESSAGE='private diagnostic'; END IF;
  IF input->>'mode'='data_error' THEN PERFORM (input->>'bad_integer')::integer; END IF;
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

CREATE SEQUENCE pgxio_private.input_calls;
GRANT USAGE ON SEQUENCE pgxio_private.input_calls TO pgxio_executor;
CREATE FUNCTION web.input_probe(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxio_private AS $$
BEGIN
  PERFORM nextval('pgxio_private.input_calls');
  RETURN jsonb_build_object('pid',pg_backend_pid(),'kind',jsonb_typeof(input),
    'number',input->>'n','text',input->>'text');
END $$;
ALTER FUNCTION web.input_probe(jsonb) OWNER TO pgxio_executor;
REVOKE ALL ON FUNCTION web.input_probe(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.input_probe(jsonb) TO web;

-- Valid JSONB outputs with predictable bytes for raw-response tests.
CREATE FUNCTION web.output_probe(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
  IF input ? 'size' THEN RETURN to_jsonb(repeat('x', (input->>'size')::integer - 2)); END IF;
  IF input->>'mode'='sql_null' THEN RETURN NULL; END IF;
  IF input->>'mode'='json_null' THEN RETURN 'null'::jsonb; END IF;
  RETURN '{"n":1844674407370955161700000000000000000000,"unicode":"a😀","escape":"a\\nb","scaled":1.2300}'::jsonb;
END $$;
REVOKE ALL ON FUNCTION web.output_probe(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.output_probe(jsonb) TO web;
CREATE FUNCTION web.output_wrong_type(jsonb) RETURNS text LANGUAGE sql AS $$ SELECT '{"looks":"valid"}'::text $$;
CREATE FUNCTION web.output_many_rows(jsonb) RETURNS SETOF jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb UNION ALL SELECT '{}'::jsonb $$;
CREATE FUNCTION web.output_no_rows(jsonb) RETURNS SETOF jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb WHERE false $$;
REVOKE ALL ON FUNCTION web.output_wrong_type(jsonb), web.output_many_rows(jsonb), web.output_no_rows(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.output_wrong_type(jsonb), web.output_many_rows(jsonb), web.output_no_rows(jsonb) TO web;
