import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pgxbench.campaign import expand_cpuset, matrix
from pgxbench.auth_compare import jobs as auth_jobs
from pgxbench.common import public_settings, subject, route
from pgxbench.fixtures import contract, seed_sql
from pgxbench.load import Driver, Histogram, Metrics, valid_response
from pgxbench.report import assess, make_report, qualify
from pgxbench.resources import key_values, summarize
from pgxbench.setup import prepare, render
from pgxbench.wrk import lua_string, parse_output, postflight


class Measurements(unittest.TestCase):
    def test_histogram_quantiles_and_bounded_retention(self):
        histogram = Histogram()
        for _ in range(20):
            for value in range(1, 1001):
                histogram.add(value)
        for q in (.5, .95, .99):
            self.assertAlmostEqual(histogram.quantile(q), q * 1000, delta=q * 1000 * .011)
        self.assertLess(len(histogram.bins), 1000)
        self.assertEqual(histogram.count, 20000)

    def test_empty_histogram_is_missing_not_zero_latency(self):
        self.assertIsNone(Histogram().summary()["p99_ms"])

    def test_pss_parser_skips_smaps_mapping_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollup"
            path.write_text("00000000-ffffff ---p 00000000 00:00 0 [rollup]\nRss: 40 kB\nPss: 20 kB\n")
            self.assertEqual(key_values(path), {"Rss": 40, "Pss": 20})

    def test_fixture_response_types_and_counts(self):
        row = {"id": 1, "version": 1, "payload": "x"}
        self.assertTrue(valid_response("bench_one", row))
        self.assertFalse(valid_response("bench_one", {**row, "id": True}))
        self.assertFalse(valid_response("bench_one", {**row, "version": 0}))
        self.assertFalse(valid_response("bench_one", {**row, "extra": "bad"}))
        self.assertTrue(valid_response("bench_page", {"rows": [row] * 100}))
        self.assertFalse(valid_response("bench_page", {"rows": []}))

    def test_wrk_statuses_must_match_completed_requests(self):
        data = {"requests": 100, "statuses": {"200": 60, "503": 40}}
        self.assertEqual(parse_output("noise\nPGXBENCH_JSON " + json.dumps(data))["statuses"]["503"], 40)
        data["requests"] = 101
        with self.assertRaises(RuntimeError):
            parse_output("PGXBENCH_JSON " + json.dumps(data))

    def test_wrk_requires_exactly_one_structured_result(self):
        with self.assertRaises(RuntimeError):
            parse_output("Running 1s test")

    def test_lua_config_escapes_every_byte(self):
        self.assertEqual(lua_string('"\n\\'), '"\\034\\010\\092"')

    def test_cpu_ranges_are_validated_and_deduplicated(self):
        self.assertEqual(expand_cpuset("2-4,3,7"), [2, 3, 4, 7])
        for value in ("-1", "4-2", "", "1-2-3"):
            with self.assertRaises(ValueError):
                expand_cpuset(value)

    def test_seed_refuses_invalid_or_injected_dimensions(self):
        for values in ((9999, 100, 128), (100, 100, 128), (10000, 100, 8193)):
            with self.assertRaises(ValueError):
                seed_sql(*values)

    def test_contract_has_read_only_and_bounded_output(self):
        endpoints = {e["name"]: e for e in contract()["endpoints"]}
        self.assertTrue(endpoints["bench_page"]["read_only"])
        self.assertFalse(endpoints["bench_write"]["read_only"])
        self.assertEqual(endpoints["bench_page"]["output"]["properties"]["rows"]["maxItems"], 100)
        self.assertNotEqual(subject(0), route(0))


class Generator(unittest.IsolatedAsyncioTestCase):
    async def test_postflight_retries_pool_recovery_outside_measurement(self):
        driver = argparse.Namespace(args=argparse.Namespace(timeout=1), preflight=AsyncMock(
            side_effect=[RuntimeError('preflight fixture/authentication failed (http_503, http_503)'), {'ok': True}]))
        result, retries = await postflight(driver)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(retries, 1)

    async def test_postflight_does_not_retry_authentication_failure(self):
        driver = argparse.Namespace(args=argparse.Namespace(timeout=1), preflight=AsyncMock(
            side_effect=RuntimeError('preflight fixture/authentication failed (http_401, http_401)')))
        with self.assertRaises(RuntimeError):
            await postflight(driver)
        self.assertEqual(driver.preflight.await_count, 1)

    async def test_postflight_does_not_hide_mixed_auth_and_pool_failures(self):
        driver = argparse.Namespace(args=argparse.Namespace(timeout=1), preflight=AsyncMock(
            side_effect=RuntimeError('preflight fixture/authentication failed (http_503, http_401)')))
        with self.assertRaises(RuntimeError):
            await postflight(driver)
        self.assertEqual(driver.preflight.await_count, 1)

    async def test_paired_authentication_keeps_body_and_route_identical(self):
        import httpx
        from pgxbench.campaign import run_namespace
        captured = []
        def handler(request):
            captured.append((request.url.path, request.headers.get('authorization'), json.loads(request.content)))
            return httpx.Response(200, json={'id': 1, 'version': 1, 'payload': 'x'})
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            prepare(workdir)
            for mode in ('jwt', 'none'):
                render(workdir, {'target': 'pgxio', 'auth_fixture': 'public', 'auth_mode': mode})
                config = run_namespace(workdir, workdir / 'result.json', dict(target='pgxio', workload='one',
                    rate=1, duration=1, warmup=0, sockets=0, subscriptions=1, active_tenants=2,
                    realtime_mode='both', probe_rate=1))
                driver = Driver(config)
                await driver.client.aclose()
                driver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                self.assertEqual((await driver.call('bench_one', {'id': 1}))[0], 'http_200')
                await driver.close()
        self.assertEqual(captured[0][0], '/v1/bench_public_one')
        self.assertEqual(captured[0][0], captured[1][0])
        self.assertEqual(captured[0][2], captured[1][2])
        self.assertTrue(captured[0][1].startswith('Bearer '))
        self.assertIsNone(captured[1][1])

    async def test_open_arrival_counts_overload_instead_of_queuing(self):
        driver = object.__new__(Driver)
        driver.args = argparse.Namespace(max_inflight=1, workload="page", active_tenants=2, sockets=0)
        driver.metrics = Metrics()
        import random
        driver.random = random.Random(1)
        active = 0
        peak = 0
        async def slow(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.03)
            active -= 1
        driver.call = slow
        import time
        started = time.perf_counter()
        await driver.arrivals(.1, 1000)
        self.assertGreaterEqual(time.perf_counter() - started, .1)
        self.assertEqual(peak, 1)
        self.assertGreater(driver.metrics.counts["dropped_inflight_limit"], 10)


class GeneratedStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workdir = Path(cls.temporary.name).resolve()
        prepare(cls.workdir)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_secrets_do_not_enter_public_settings(self):
        settings = json.loads((self.workdir / "settings.json").read_text())
        public = json.dumps(public_settings(settings))
        for key in ("admin_password", "web_password", "realtime_secret", "secret_key_base"):
            self.assertNotIn(settings[key], public)
        self.assertEqual((self.workdir / "settings.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.workdir / "jwt-private.pem").stat().st_mode & 0o777, 0o600)

    def test_no_jwt_requires_public_fixture_and_explicit_benchmark_image(self):
        with self.assertRaises(ValueError):
            render(self.workdir, {'auth_mode': 'none', 'auth_fixture': 'tenant'})
        render(self.workdir, {'target': 'pgxio', 'auth_mode': 'none', 'auth_fixture': 'public'})
        services = json.loads((self.workdir / 'compose.json').read_text())['services']
        self.assertEqual(services['pgxio']['image'], 'pgxio:bench-auth')
        self.assertEqual(services['pgxio']['build']['args']['CARGO_FEATURES'], 'benchmark-auth')
        render(self.workdir, {'target': 'pgxio', 'auth_mode': 'jwt', 'auth_fixture': 'tenant'})
        module = json.loads((self.workdir / 'runtime/pgxio.json').read_text())
        self.assertNotIn('benchmark_no_jwt', module)

    def test_fixed_pool_total_does_not_increase_with_workers(self):
        for cpus in (1, 2, 4, 8):
            settings = render(self.workdir, {"target": "pgxio", "cpus": cpus, "workers": cpus,
                "pool_mode": "fixed", "memory_mib": 0})
            self.assertEqual(settings["pool_total"], 8)
            self.assertEqual(settings["pool_per_worker"], 8 // cpus)

    def test_growing_pool_and_go_parallelism(self):
        settings = render(self.workdir, {"target": "go", "cpus": 4, "workers": 4, "pool_mode": "growing"})
        services = json.loads((self.workdir / "compose.json").read_text())["services"]
        self.assertEqual(settings["pool_total"], 16)
        self.assertEqual(services["go"]["environment"]["POOL_SIZE"], "16")
        self.assertEqual(services["go"]["environment"]["GOMAXPROCS"], "4")

    def test_multicontainer_cap_requires_shared_parent(self):
        for target in ("postgrest", "supabase"):
            with self.assertRaisesRegex(ValueError, "multi-service"):
                render(self.workdir, {"target": target, "frontend": "tls", "memory_mib": 64, "cgroup_parent": "", "cgroup_path": ""})

    def test_http_postgrest_is_direct_and_accepts_single_service_cap(self):
        render(self.workdir, {"target": "postgrest", "frontend": "http", "memory_mib": 64,
                              "cgroup_parent": "", "cgroup_path": ""})
        services = json.loads((self.workdir / "compose.json").read_text())["services"]
        self.assertNotIn("proxy", services)
        self.assertIn("127.0.0.1:18080:3000", services["postgrest"]["ports"])
        self.assertEqual(services["postgrest"]["mem_limit"], "64m")
        self.assertNotIn("listen 8443", (self.workdir / "runtime/nginx.conf").read_text())

    def test_api_mount_excludes_signing_and_database_server_keys(self):
        render(self.workdir, {"target": "node", "memory_mib": 0, "cpus": 1, "workers": 1})
        config = json.loads((self.workdir / "compose.json").read_text())
        mounts = config["services"]["node"]["volumes"]
        self.assertEqual(mounts, [str(self.workdir / "runtime") + ":/bench:ro"])
        self.assertFalse((self.workdir / "runtime" / "jwt-private.pem").exists())
        self.assertFalse((self.workdir / "runtime" / "key.pem").exists())
        self.assertIn("sslmode=verify-full", config["services"]["node"]["environment"]["DATABASE_URL"])


def passing_run():
    return {"completed": True, "preflight": {"passed": True}, "workload": {"sockets": 0},
        "results": {"counts": {"http_200": 10000}, "http_latency": {"p95_ms": 10}}}


class Qualification(unittest.TestCase):
    def test_reports_keep_distinct_wrk_concurrency_cases_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for connections in (1, 16):
                run = passing_run()
                run.update(kind="competitive_benchmark", target="pgxio", transport="http/ws", engine="wrk",
                           configuration={"cpus": 1, "workers": 1, "pool_mode": "fixed", "pool_total": 8, "memory_mib": 0},
                           wrk={"connections": connections, "threads": 1})
                run["workload"].update(workload="page", rate=0, duration=30, sockets=0, subscriptions=1)
                run["results"].update(successful_requests_per_second=100,
                    notification_latency=Histogram().summary(), fresh_data_latency=Histogram().summary())
                run["results"]["http_latency"]["p99_ms"] = 20
                (root / f"{connections}.json").write_text(json.dumps(run))
            make_report(root, root / "report")
            aggregates = json.loads((root / "report/aggregates.json").read_text())
            self.assertEqual(len(aggregates), 2)
            rows = json.loads((root / 'report/summary.json').read_text())['rows']
            self.assertEqual(len({row['profile_id'] for row in rows}), 2)
            self.assertEqual({g["profile"]["engine_settings"]["connections"] for g in aggregates}, {1, 16})

    def test_load_only_pass_does_not_qualify_ram(self):
        result = assess(passing_run())
        self.assertTrue(result["workload_passed"])
        self.assertFalse(result["resource_evidence_valid"])
        self.assertFalse(result["minimum_RAM_qualified"])

    def test_fast_errors_do_not_count_as_capacity(self):
        run = passing_run()
        run["results"]["counts"] = {"http_200": 1, "http_503": 9999}
        self.assertFalse(assess(run)["workload_passed"])

    def test_dropped_offered_load_fails(self):
        run = passing_run()
        run["results"]["counts"]["dropped_inflight_limit"] = 100
        self.assertIn("offered_load_not_scheduled", assess(run)["reasons"])

    def test_wrk2_uncompleted_offered_load_fails(self):
        run = passing_run()
        run["results"]["counts"]["offered_minus_completed"] = 1000
        self.assertFalse(assess(run)["workload_passed"])

    def test_resource_file_cannot_be_reused_for_other_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resources.jsonl"
            path.write_text(json.dumps({"type": "metadata", "configuration": {"target": "go"}}) + "\n")
            run = {"started_unix": 1, "finished_unix": 2, "configuration": {"target": "pgxio"},
                   "results": {"counts": {"http_200": 10}}}
            result = summarize(path, run)
            self.assertFalse(result["valid"])
            self.assertIn("configuration_mismatch", result["errors"])

    def resource_fixture(self, folder, cap=64, gap=False):
        config = {"target": "pgxio", "memory_mib": 64}
        run = {"target": "pgxio", "started_unix": 0, "finished_unix": 12, "measurement_started_unix": 1,
               "configuration": config, "results": {"counts": {"http_200": 10}}}
        path = Path(folder) / "resource.jsonl"
        records = [{"type": "metadata", "configuration": config}]
        for t in ([1,10] if gap else range(1,11)):
            records.append({"type": "sample", "timestamp": t, "groups": {"pgxio": {
                "container_id": "test-api", "role": "api", "pss_bytes": 1024, "rss_bytes": 2048,
                "memory_current_bytes": 4096, "memory_max": str(cap*1048576), "swap_max": "0",
                "memory_events": {}, "cpu_stat": {"usage_usec": t*100000}}}, "errors": []})
        path.write_text("\n".join(json.dumps(record) for record in records))
        return path, run

    def test_cap_must_match_the_requested_total(self):
        with tempfile.TemporaryDirectory() as folder:
            path, run = self.resource_fixture(folder, cap=128)
            result = summarize(path, run)
            self.assertFalse(result["valid"])
            self.assertIn("requested_memory_cap_not_enforced", result["errors"])

    def test_valid_resource_trace_preserves_pss_and_cpu_units(self):
        with tempfile.TemporaryDirectory() as folder:
            path, run = self.resource_fixture(folder)
            result = summarize(path, run)
            self.assertTrue(result["valid"])
            self.assertTrue(result["requested_memory_cap_enforced"])
            self.assertEqual(result["peaks"]["api_pss_bytes"],1024)
            self.assertAlmostEqual(result["api_cpu_ms_per_success"],90)

    def test_sampling_gaps_invalidate_resource_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path, run = self.resource_fixture(folder, gap=True)
            self.assertIn("resource_sampling_gap", summarize(path, run)["errors"])

    def test_empty_results_produce_reviewable_reports_without_ram_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            results = Path(folder) / "results"
            results.mkdir()
            output = Path(folder) / "report"
            self.assertEqual(make_report(results, output)["trials"],0)
            self.assertTrue((output / "charts.html").exists())
            self.assertIn("No percentile", (output / "charts.html").read_text())
            result = qualify(results, output / "ram.json")
            self.assertEqual(result["api_floors"],[])
            self.assertEqual(result["whole_VPS_floors"],[])


class Campaign(unittest.TestCase):
    def test_auth_comparison_randomizes_matched_profiles_and_separates_cache_case(self):
        args = argparse.Namespace(targets='pgxio,postgrest', repetitions=3, warmup=10, duration=30,
                                  seed=7, connections=[1,8], wrk_image='wrk', wrk_cpuset='2-3')
        cases = auth_jobs(args)
        self.assertEqual(cases, auth_jobs(args))
        self.assertEqual(len(cases), 30)
        self.assertTrue(all(j['auth_fixture'] == 'public' for j in cases))
        self.assertTrue(all(j['target'] == 'postgrest' and j['auth_mode'] == 'jwt'
                            for j in cases if j['jwt_cache'] == 'default'))

    def test_reproducible_randomization_and_trial_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(targets="pgxio", repetitions=None, duration=None, kind="ram",
                memory_values=[32, 48, 64], rates=[50], seed=7, warmup=120, rows=10000,
                output=Path(temporary) / "jobs.json")
            matrix(args)
            first = args.output.read_text()
            matrix(args)
            self.assertEqual(first, args.output.read_text())
            jobs = json.loads(first)["jobs"]
            self.assertEqual(len(jobs), 9)
            self.assertTrue(all(j["duration"] == 3600 and j["sockets"] == 100 for j in jobs))



class PerformanceProfiles(unittest.TestCase):
    def test_stage_images_and_options_do_not_leak_into_legacy_profile(self):
        from pgxbench.perf_compare import jobs
        args=argparse.Namespace(profiles='baseline,recovery,prepared,pipeline,queue,cache,postgrest-cached',
            workloads='one,page,write,mixed',nginx_profiles='baseline',connections=[1,8,32],repetitions=3,
            baseline_image='pgxio:old',image='pgxio:new',cpus=1,workers=None,wrk_image='wrk',wrk_cpuset=None,
            duration=30,warmup=10,seed=17)
        result=jobs(args);self.assertEqual(len(result),252);self.assertEqual(result,jobs(args))
        stages={job['profile']:job for job in result}
        self.assertEqual(stages['baseline']['module_options'],{})
        self.assertEqual(stages['baseline']['pgxio_image'],'pgxio:old')
        self.assertFalse(stages['recovery']['module_options']['db_prepared_statements'])
        self.assertFalse(stages['prepared']['module_options']['db_pipeline'])
        self.assertTrue(stages['pipeline']['module_options']['db_pipeline'])
        self.assertEqual(stages['queue']['module_options']['pool_acquisition_timeout_ms'],1000)
        self.assertEqual(stages['cache']['module_options']['jwt_cache_max_entries'],1000)
        self.assertEqual(stages['postgrest-cached']['target'],'postgrest')

    def test_render_existing_stage_image_does_not_rebuild_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary);prepare(path)
            render(path,{'target':'pgxio','frontend':'http','auth_fixture':'public','pgxio_image':'pgxio:old',
                         'module_options':{},'nginx_profile':'reuseport'})
            service=json.loads((path/'compose.json').read_text())['services']['pgxio']
            self.assertNotIn('build',service);self.assertEqual(service['image'],'pgxio:old')
            self.assertNotIn('db_pipeline',json.loads((path/'runtime/pgxio.json').read_text()))
            self.assertIn('listen 8080 reuseport;', (path/'runtime/nginx.conf').read_text())
            render(path,{'module_options':{'db_pipeline':False},'nginx_profile':'keepalive-10000'})
            self.assertFalse(json.loads((path/'runtime/pgxio.json').read_text())['db_pipeline'])
            self.assertIn('keepalive_requests 10000;', (path/'runtime/nginx.conf').read_text())


if __name__ == "__main__":
    unittest.main()
