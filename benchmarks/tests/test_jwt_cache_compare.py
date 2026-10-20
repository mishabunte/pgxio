import importlib.util
from collections import Counter
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('jwt_cache_compare',Path(__file__).resolve().parents[1]/'jwt-cache-compare.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class JwtCacheComparisonTests(unittest.TestCase):
    def test_matrix_and_actual_pgxio_options(self):
        jobs=module.jobs()
        self.assertEqual(len(jobs),24)
        self.assertEqual(set(Counter((j['target'],j['jwt_cache'],j['workload']) for j in jobs).values()),{3})
        for job in jobs:
            self.assertEqual(job['auth_mode'],'jwt')
            self.assertEqual(job['connections'],32)
            self.assertEqual(job['duration'],30)
            self.assertEqual(job['request_bytes'],1024 if job['workload']=='page' else 65536)
            if job['target']=='pgxio':
                self.assertEqual(job['module_options']['jwt_cache_max_entries'],1000 if job['jwt_cache']=='default' else 0)
                self.assertEqual(job['module_options']['jwt_cache_max_bytes'],1048576)
                self.assertEqual(job['module_options']['libpq_buf'],'copy')


if __name__=='__main__':unittest.main()
