-- Bootstrap/migration credentials ONLY. Never run this as the web role.
-- psql -v web_password=... -f sql/example.sql
\set ON_ERROR_STOP on
BEGIN;
CREATE ROLE web LOGIN PASSWORD :'web_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE pgxio_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA web;
CREATE SCHEMA pgxio_private;
REVOKE ALL ON SCHEMA web, pgxio_private FROM PUBLIC;
GRANT USAGE ON SCHEMA web TO web;
GRANT USAGE ON SCHEMA pgxio_private TO pgxio_executor;
ALTER DEFAULT PRIVILEGES IN SCHEMA web REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE pgxio_private.accounts (
    subject text PRIMARY KEY,
    route uuid NOT NULL DEFAULT gen_random_uuid(),
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE pgxio_private.tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject text NOT NULL REFERENCES pgxio_private.accounts(subject),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    done boolean NOT NULL DEFAULT false
);
GRANT SELECT ON pgxio_private.accounts TO pgxio_executor;
GRANT SELECT, INSERT, UPDATE ON pgxio_private.tasks TO pgxio_executor;

CREATE FUNCTION web.list_tasks(input jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pgxio_private
AS $$
BEGIN
    IF input IS DISTINCT FROM '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='PT400', MESSAGE='invalid input';
    END IF;
    RETURN (SELECT jsonb_build_object('tasks',coalesce(jsonb_agg(jsonb_build_object('id',t.id,'title',t.title,'done',t.done) ORDER BY t.id),'[]'::jsonb))
    FROM (SELECT t.* FROM pgxio_private.tasks t
      JOIN pgxio_private.accounts a USING (subject)
      WHERE a.enabled AND t.subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'
      ORDER BY t.id LIMIT 100) t);
END
$$;

CREATE FUNCTION web.create_task(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pgxio_private
AS $$
DECLARE account pgxio_private.accounts; task pgxio_private.tasks;
BEGIN
    IF jsonb_typeof(input) IS DISTINCT FROM 'object'
       OR jsonb_typeof(input->'title') IS DISTINCT FROM 'string'
       OR length(input->>'title') NOT BETWEEN 1 AND 500
       OR input - 'title' <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='PT400', MESSAGE='invalid input';
    END IF;
    SELECT * INTO STRICT account FROM pgxio_private.accounts
      WHERE enabled AND subject=current_setting('request.jwt.claims',true)::jsonb->>'sub';
    INSERT INTO pgxio_private.tasks(subject,title) VALUES(account.subject,input->>'title') RETURNING * INTO task;
    PERFORM pg_notify('pgxio_events',account.route::text);
    RETURN jsonb_build_object('id',task.id,'title',task.title,'done',task.done);
END
$$;

CREATE FUNCTION web.authorize_tasks(input jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pgxio_private
AS $$
    SELECT coalesce((SELECT to_jsonb(route::text) FROM pgxio_private.accounts
      WHERE enabled AND subject=current_setting('request.jwt.claims',true)::jsonb->>'sub'),'null'::jsonb)
$$;

ALTER FUNCTION web.list_tasks(jsonb) OWNER TO pgxio_executor;
ALTER FUNCTION web.create_task(jsonb) OWNER TO pgxio_executor;
ALTER FUNCTION web.authorize_tasks(jsonb) OWNER TO pgxio_executor;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA web FROM PUBLIC;
GRANT EXECUTE ON FUNCTION web.list_tasks(jsonb),web.create_task(jsonb),web.authorize_tasks(jsonb) TO web;
-- Example subjects only. Production provisioning belongs in your migrations.
INSERT INTO pgxio_private.accounts(subject) VALUES ('alice'),('bob');
COMMIT;
