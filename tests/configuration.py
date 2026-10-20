"""Exercise Nginx's real configuration parser against native pgxio directives."""
import os
from pathlib import Path
import subprocess
import unittest
from integration import COMPOSE


class Configuration(unittest.TestCase):
    def check(self, snippet):
        runtime = Path(os.environ['PGXIO_TEST_RUNTIME'])
        original = (runtime/'nginx.conf').read_text()
        config_dir = os.environ.get('PGXIO_TEST_CONFIG_DIR', '/etc/pgxio')
        text = original.replace('include '+config_dir+'/pgxio.conf;', snippet.replace('/etc/pgxio/', config_dir+'/'))
        (runtime/'check.conf').write_text(text)
        return subprocess.run(COMPOSE+['exec','-T','api','nginx','-t','-c','/etc/pgxio/check.conf'],capture_output=True,text=True)

    def test_valid_config_and_required_settings(self):
        base='pgxio_issuer test; pgxio_audience test; pgxio_endpoint hello web.hello;'
        self.assertEqual(self.check(base).returncode,0)
        for invalid in [base+' pgxio_pool_size 0;', base+' pgxio_issuer duplicate;',
                        base+' pgxio_libpq_buf invalid;', base+' pgxio_db_borrowed_buffers on;',
                        base+' pgxio_libpq_buf auto; pgxio_libpq_buf copy;',
                        base+' pgxio_endpoint hello web.other;', base+' pgxio_endpoint x private.secret;',
                        base+' pgxio_validate_output on;', base+' pgxio_validate_input on;',
                        base+' pgxio_validate_output true;', base+' pgxio_unknown x;',
                        'pgxio_config /etc/pgxio/pgxio.json;', 'pgxio_endpoint hello web.hello;']:
            self.assertNotEqual(self.check(invalid).returncode,0,invalid)
        forced = self.check(base+' pgxio_libpq_buf borrowed;')
        self.assertEqual(forced.returncode == 0, os.environ.get('PGXIO_TEST_PATCHED') == '1', forced.stderr)
        for mode in ('auto', 'copy'):
            self.assertEqual(self.check(base+f' pgxio_libpq_buf {mode};').returncode,0)

    def test_invalid_reload_keeps_serving(self):
        import time
        import httpx
        from integration import URL
        runtime = Path(os.environ['PGXIO_TEST_RUNTIME'])
        path = runtime/'pgxio.conf'
        original = path.read_text()
        try:
            path.write_text(original+'\npgxio_pool_size 0;\n')
            subprocess.run(COMPOSE+['kill','-s','HUP','api'],check=True,capture_output=True)
            time.sleep(.5)
            self.assertEqual(httpx.get(URL+'/readyz').status_code,200)
            logs=subprocess.run(COMPOSE+['logs','--no-color','api'],capture_output=True,text=True,check=True).stdout
            self.assertIn('invalid or duplicate pgxio_pool_size',logs)
        finally:
            path.write_text(original)

    def test_contract_is_optional_but_is_checked_when_present(self):
        runtime = Path(os.environ['PGXIO_TEST_RUNTIME'])
        import json
        (runtime/'bad-contract.json').write_text(json.dumps({'version':2,'endpoints':[{'name':'hello','input':{'$ref':'https://example.org/schema'},'output':{}}]}))
        base='pgxio_issuer test; pgxio_audience test; pgxio_endpoint hello web.hello;'
        self.assertNotEqual(self.check(base+' pgxio_contract /etc/pgxio/bad-contract.json;').returncode,0)
        (runtime/'bad-contract.json').write_text(json.dumps({'version':2,'endpoints':[{'name':'unknown','input':{},'output':{}}]}))
        self.assertNotEqual(self.check(base+' pgxio_contract /etc/pgxio/bad-contract.json;').returncode,0)
        (runtime/'bad-contract.json').write_text(json.dumps({'version':2,'endpoints':[{'name':'hello','input':{},'output':{}}]}))
        self.assertEqual(self.check(base+' pgxio_contract /etc/pgxio/bad-contract.json; pgxio_validate_input on; pgxio_validate_output on;').returncode,0)


if __name__ == '__main__': unittest.main(verbosity=2)
