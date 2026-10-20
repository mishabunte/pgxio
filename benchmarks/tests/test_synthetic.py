import importlib.util
from collections import Counter
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('synthetic_runner',Path(__file__).resolve().parents[1]/'run-synthetic.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SyntheticMatrixTests(unittest.TestCase):
    def test_exactly_paired_modes_and_five_repetitions(self):
        rows=runner.cases()
        self.assertEqual(len(rows),750)
        counts=Counter((r['stage'],r['transport'],r['body_bytes'],r['segments'],r['mode']) for r in rows)
        self.assertEqual(set(counts.values()),{5})
        for key in counts:
            self.assertIn((*key[:-1],'borrowed' if key[-1]=='copy' else 'copy'),counts)
        self.assertTrue(all(r['transport']=='plain' for r in rows if r['stage']=='input'))
        self.assertEqual(rows,runner.cases())

    def test_smoke_is_small_but_covers_both_transports(self):
        rows=runner.cases(1,True)
        self.assertEqual(len(rows),20)
        self.assertEqual({r['body_bytes'] for r in rows},{1024})
        self.assertEqual({r['segments'] for r in rows},{1,16})
        self.assertEqual({r['transport'] for r in rows if r['stage']=='combined'},{'plain','tls'})

    def test_auto_matrix_includes_threshold_and_fragmentation_cases(self):
        rows=runner.cases(1,True,True)
        self.assertEqual(len(rows),90)
        self.assertEqual({r['mode'] for r in rows},{'copy','borrowed','auto'})
        self.assertEqual({r['body_bytes'] for r in rows},{1024,16384,65536})


if __name__=='__main__':unittest.main()
