"""HTTP output ownership, result type, size, precision and commit boundaries."""
import unittest
import httpx
from integration import URL, sql, token


class Output(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = httpx.AsyncClient(base_url=URL, timeout=10)
        self.headers = {'Authorization': 'Bearer ' + token()}

    async def asyncTearDown(self):
        await self.http.aclose()

    async def call(self, value=None, endpoint='output_probe'):
        return await self.http.post('/v1/' + endpoint, json=value or {}, headers=self.headers)

    async def test_postgresql_bytes_survive_commit_without_normalization(self):
        response = await self.call()
        self.assertEqual(response.status_code, 200, response.text)
        expected = sql("SELECT web.output_probe('{}'::jsonb)::text;").encode()
        self.assertEqual(response.content, expected)
        self.assertIn(b'1844674407370955161700000000000000000000', response.content)
        self.assertIn(b'1.2300', response.content)

    async def test_size_limit_and_json_null(self):
        for size in (1024, 16384, 65536, 1048576):
            response = await self.call({'size':size})
            self.assertEqual(response.status_code, 200, response.text[:100])
            self.assertEqual(len(response.content), size)
        self.assertEqual((await self.call({'size':1048577})).status_code,503)
        response = await self.call({'mode':'json_null'})
        self.assertEqual((response.status_code, response.content), (200, b'null'))

    async def test_invalid_result_shapes_are_rejected_and_pool_recovers(self):
        for endpoint, value in [('output_probe', {'mode':'sql_null'}),
                                ('output_wrong_type', {}), ('output_many_rows', {}), ('output_no_rows', {})]:
            response = await self.call(value, endpoint)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json(), {'error':{'code':'database_error'}})
            self.assertEqual((await self.call()).status_code, 200)

    async def test_commit_failure_cannot_send_a_success_body(self):
        # Deferred constraint fires at COMMIT, after the function produced JSONB.
        sql("CREATE TABLE pgxio_private.commit_probe(id integer UNIQUE DEFERRABLE INITIALLY DEFERRED);")
        sql("INSERT INTO pgxio_private.commit_probe VALUES(1);")
        sql("CREATE SEQUENCE pgxio_private.commit_probe_calls;")
        sql("SELECT nextval('pgxio_private.commit_probe_calls');")
        original = sql("SELECT pg_get_functiondef('web.output_probe(jsonb)'::regprocedure);")
        try:
            sql("CREATE OR REPLACE FUNCTION web.output_probe(input jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$ BEGIN INSERT INTO pgxio_private.commit_probe VALUES(1); PERFORM nextval('pgxio_private.commit_probe_calls'); RETURN '{\"must_not_escape\":true}'::jsonb; END $$;")
            response = await self.call()
            self.assertEqual(response.status_code,503)
            self.assertNotIn('must_not_escape',response.text)
            self.assertEqual(sql('SELECT last_value FROM pgxio_private.commit_probe_calls;'), '2')
            self.assertEqual(sql('SELECT count(*) FROM pgxio_private.commit_probe;'), '1')
        finally:
            sql(original)
            sql('DROP TABLE pgxio_private.commit_probe; DROP SEQUENCE pgxio_private.commit_probe_calls;')
        self.assertEqual((await self.call()).status_code, 200)


if __name__ == '__main__': unittest.main(verbosity=2)
