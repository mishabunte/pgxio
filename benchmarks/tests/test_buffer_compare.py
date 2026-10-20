import importlib.util
from collections import Counter
from pathlib import Path
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location('buffer_compare', Path(__file__).resolve().parents[1] / 'buffer-compare.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class BufferCampaign(unittest.TestCase):
    def test_paired_cases_and_stable_resume_ids(self):
        five = runner.jobs('frozen-image', 5)
        seven = runner.jobs('frozen-image', 7)
        self.assertEqual(len(five), 60)
        self.assertEqual(len(seven), 84)
        counts = Counter((r['database_tls'], r['request_bytes'], r['profile']) for r in five)
        self.assertEqual(len(counts), 12)
        self.assertEqual(set(counts.values()), {5})
        extended = {r['id']: r for r in seven}
        self.assertEqual(len(extended), 84)
        for row in five:
            self.assertEqual(row, extended[row['id']])
            self.assertEqual(row['module_options']['libpq_buf'], row['profile'].removeprefix('buffer-'))
            self.assertEqual((row['workload'], row['jwt_cache'], row['connections']), ('one', 'off', 32))
        self.assertEqual(five, runner.jobs('frozen-image', 5))
