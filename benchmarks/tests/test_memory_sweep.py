"""Boundary evidence must not turn a search limit into a claimed minimum."""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('memory_sweep',Path(__file__).resolve().parents[1]/'memory-sweep.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MemoryBoundaryTests(unittest.TestCase):
    def test_unbounded_and_adjacent_failure(self):
        self.assertEqual(module.bounded_floor({16:True,32:True}),(16,False))
        self.assertEqual(module.bounded_floor({8:False,16:True,32:True}),(16,True))
        self.assertEqual(module.bounded_floor({8:False,32:True}),(32,False))
        self.assertEqual(module.bounded_floor({16:False}),(None,False))

    def test_retention_threshold(self):
        self.assertTrue(module.retention_pass(900,1000))
        self.assertFalse(module.retention_pass(899,1000))
        self.assertFalse(module.retention_pass(900,0))

    def test_generator_misses_are_not_memory_failures(self):
        self.assertTrue(module.generator_missed({'results':{'counts':{'http_200':1477,'unscheduled':23}}}))
        self.assertFalse(module.generator_missed({'results':{'counts':{'http_200':1500}}}))

    def test_load_tolerance_does_not_waive_http_failures(self):
        offered='achieved_throughput_below_99.9_percent_of_wrk2_offered_rate'
        run={'results':{'successful_requests_per_second':49.73}}
        self.assertTrue(module.ram_assessment({'reasons':[offered]},run,'light')['workload_passed'])
        self.assertFalse(module.ram_assessment({'reasons':[offered,'HTTP_success_below_99.9_percent']},run,'light')['workload_passed'])
        run['results']['successful_requests_per_second']=49.4
        self.assertFalse(module.ram_assessment({'reasons':[offered]},run,'light')['workload_passed'])


if __name__=='__main__':
    unittest.main()
