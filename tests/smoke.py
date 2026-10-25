"""Production build smoke: no diagnostic endpoints or authentication bypass."""
import unittest
import time
import httpx
from integration import URL, token


class Smoke(unittest.TestCase):
    def setUp(self):
        for _ in range(100):
            try:
                if httpx.get(URL+'/readyz').status_code==200: return
            except httpx.HTTPError: pass
            time.sleep(.05)
        self.fail('API did not become ready')

    def test_production_module(self):
        self.assertEqual(httpx.get(URL+'/readyz').status_code, 200)
        response=httpx.get(URL+'/__pgxio_buffers')
        self.assertEqual(response.status_code,405)
        self.assertEqual(response.json(),{'error':{'code':'method_not_allowed'}})
        self.assertEqual(httpx.post(URL+'/v1/list_tasks', json={}).status_code, 401)
        response=httpx.post(URL+'/v1/list_tasks', json={}, headers={'Authorization':'Bearer '+token()})
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == '__main__': unittest.main()
