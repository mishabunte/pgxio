"""Combine only valid, error-free JWT comparison trials into one report."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import median
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'benchmarks'))
from pgxbench.report import make_report

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('sources',nargs='+',type=Path)
parser.add_argument('--output',type=Path,default=ROOT/'artifacts/jwt-cache-db4-summary')
args=parser.parse_args()
output=args.output
output.mkdir(parents=True,exist_ok=True)
expected={j['id']:j for j in json.loads((args.sources[0]/'campaign.json').read_text())['jobs']}
assert len(expected)==24,'first source must contain full campaign'
selected={}
evidence=[]
for source in args.sources:
    environment=json.loads((source/'environment.json').read_text())
    assert 'finished_unix' in environment
    assert environment['docker']['NCPU']==8
    for row in json.loads((source/'outcomes.json').read_text()):
        if not row['passed'] or any(k!='http_200' and v for k,v in row.get('counts',{}).items()):continue
        j=row['job']
        assert j==expected[j['id']] and j['id'] not in selected
        selected[j['id']]=row
        stem=f"{j['profile']}-{j['id']}-{j['target']}-{j['auth_mode']}-{j['jwt_cache']}-{j['connections']}c-r{j['repetition']}"
        for suffix in ('.json','.resources.jsonl'):
            original=(source/'results'/(stem+suffix)).resolve()
            assert original.exists()
            target=output/'trials'/(source.name+'-'+original.name)
            target.parent.mkdir(exist_ok=True)
            if not target.exists():target.symlink_to(original)
    evidence.append(dict(source=str(source),environment=environment,
        source_hashes=json.loads((source/'source.json').read_text()),
        checked_configuration=json.loads((source/'configuration-evidence.json').read_text())))
assert set(selected)==set(expected),'campaign incomplete'
make_report(output/'trials',output/'report')
rows=json.loads((output/'report/summary.json').read_text())['rows']
assert len(rows)==24 and all(r['workload_passed'] and r['resource_evidence_valid'] and r['HTTP_error_fraction']==0 for r in rows)
groups=defaultdict(list)
for r in rows:groups[r['workload'],r['request_bytes'],r['performance_profile']].append(r)
assert len(groups)==8 and all(len(g)==3 for g in groups.values())
fields=('success_rps','http_p95_ms','http_p99_ms','api_cpu_ms_per_success',
    'api_pss_bytes_mean','api_rss_bytes_mean','api_memory_current_bytes_mean',
    'api_cpu_percent_mean','database_pss_bytes_mean','database_cpu_percent_mean','host_load1_mean')
medians=[dict(workload=w,input_bytes=n,profile=p,trials=3,
    rps_min=min(r['success_rps'] for r in g),rps_max=max(r['success_rps'] for r in g),
    **{f:median(r[f] for r in g) for f in fields}) for (w,n,p),g in sorted(groups.items())]
(output/'medians.json').write_text(json.dumps(medians,indent=2)+'\n')
with (output/'medians.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=list(medians[0]));writer.writeheader();writer.writerows(medians)
(output/'provenance.json').write_text(json.dumps(evidence,indent=2)+'\n')
(output/'outcomes.json').write_text(json.dumps([selected[k] for k in sorted(selected)],indent=2)+'\n')
ix={(r['workload'],r['profile']):r for r in medians}
comparisons=[]
for workload in ('page','one'):
    pc=ix[workload,'pgxio-cached'];pu=ix[workload,'pgxio-uncached']
    rc=ix[workload,'postgrest-cached'];ru=ix[workload,'postgrest-uncached']
    comparisons.append(dict(workload=workload,cached_pgxio_over_postgrest=pc['success_rps']/rc['success_rps'],
        uncached_pgxio_over_postgrest=pu['success_rps']/ru['success_rps'],
        pgxio_cached_over_uncached=pc['success_rps']/pu['success_rps'],
        postgrest_cached_over_uncached=rc['success_rps']/ru['success_rps']))
(output/'comparisons.json').write_text(json.dumps(comparisons,indent=2)+'\n')
lines=['# JWT caching: pgxio versus PostgREST','',
    '24 valid, error-free trials; eight cases with three randomized repetitions each. '
    'Each trial has 10 seconds warmup and 30 seconds measurement. Tables report medians; '
    'latency entries are medians of trial percentiles, not pooled percentiles.','',
    'Eight-vCPU ARM64 Colima VM, one API core (CPU 0), four PostgreSQL cores (1–4), '
    'wrk on 5–6, pool eight, 32 HTTP connections. HTTP frontend and plaintext PostgreSQL. '
    'Same 10,000-row/100-tenant/128-byte-payload fixture as before. '
    'Page returns 100 rows with a 1 KiB JSON argument; one-row RPC receives 64 KiB. '
    'PostgREST adds its native 10-byte HTTP envelope. No API memory limit. '
    'pgxio uses the frozen experimental-libpq image with borrowed buffers disabled.','',
    'RS256 JWT verification remains enabled in every profile. Cached profiles allow 1,000 '
    'entries; pgxio additionally budgets 1 MiB for accounted claims/digests, excluding '
    'metadata overhead. Products retain their native cache expiry policies; pgxio caps '
    'entries at 30 seconds or token expiry. Ten tokens are reused. Actual settings and '
    'CPU limits were asserted before every trial. This is a repeated-token workload, '
    'not an estimate for predominantly unique tokens. Cache hit counters were not collected.','',
    '| RPC / input | Profile | RPS | RPS range | p95 ms | p99 ms | API CPU ms/request | API RAM PSS MiB | API CPU % | DB RAM PSS MiB | DB cores / 4 | Host load1 |',
    '|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for r in medians:
    lines.append(f"| {r['workload']} / {r['input_bytes']//1024} KiB | {r['profile']} | {r['success_rps']:.0f} | "
        f"{r['rps_min']:.0f}–{r['rps_max']:.0f} | {r['http_p95_ms']:.2f} | {r['http_p99_ms']:.2f} | "
        f"{r['api_cpu_ms_per_success']:.4f} | {r['api_pss_bytes_mean']/1048576:.2f} | {r['api_cpu_percent_mean']:.1f} | "
        f"{r['database_pss_bytes_mean']/1048576:.2f} | {r['database_cpu_percent_mean']/100:.2f} | {r['host_load1_mean']:.2f} |")
lines+=['','## Throughput comparisons','',
    '| RPC | pgxio / PostgREST, cached | pgxio / PostgREST, uncached | pgxio cache gain | PostgREST cache gain |',
    '|---|---:|---:|---:|---:|']
for c in comparisons:
    lines.append(f"| {c['workload']} | {c['cached_pgxio_over_postgrest']:.2f}× | {c['uncached_pgxio_over_postgrest']:.2f}× | "
        f"{(c['pgxio_cached_over_uncached']-1)*100:+.1f}% | {(c['postgrest_cached_over_uncached']-1)*100:+.1f}% |")
lines+=['','## Interpretation','',
    f"With both caches enabled, median pgxio throughput is {comparisons[0]['cached_pgxio_over_postgrest']:.2f}× PostgREST for the "
    f"page RPC and {comparisons[1]['cached_pgxio_over_postgrest']:.2f}× for the 64 KiB one-row RPC. Median cache-on throughput gains "
    f"are {(comparisons[0]['pgxio_cached_over_uncached']-1)*100:.1f}% / {(comparisons[1]['pgxio_cached_over_uncached']-1)*100:.1f}% for pgxio and "
    f"{(comparisons[0]['postgrest_cached_over_uncached']-1)*100:.1f}% / {(comparisons[1]['postgrest_cached_over_uncached']-1)*100:.1f}% for PostgREST (page / one-row). "
    'These gains describe this campaign, not guaranteed improvements.','',
    f"The lowest valid uncached pgxio page trial ({ix['page','pgxio-uncached']['rps_min']:,.0f} RPS) "
    f"remains included. Cached pgxio page trials use a median {ix['page','pgxio-cached']['database_cpu_percent_mean']/100:.2f} database cores "
    f"and {ix['page','pgxio-cached']['api_cpu_percent_mean']:.1f}% API CPU. The cached 64 KiB pgxio "
    f"median p99 is {ix['one','pgxio-cached']['http_p99_ms']:.2f} ms versus {ix['one','pgxio-uncached']['http_p99_ms']:.2f} ms uncached. "
    'Do not describe caching as an unconditional latency improvement.','',
    f"Cached API process PSS medians are {min(ix[w,'pgxio-cached']['api_pss_bytes_mean'] for w in ('page','one'))/1048576:.1f}–"
    f"{max(ix[w,'pgxio-cached']['api_pss_bytes_mean'] for w in ('page','one'))/1048576:.1f} MiB for pgxio versus "
    f"{min(ix[w,'postgrest-cached']['api_pss_bytes_mean'] for w in ('page','one'))/1048576:.1f}–"
    f"{max(ix[w,'postgrest-cached']['api_pss_bytes_mean'] for w in ('page','one'))/1048576:.1f} MiB for PostgREST. Cache overhead here reflects ten reused tokens, "
    'not a full 1,000-entry cache.','',
    'Shared local host and three repetitions per case: retain the ranges. '
    'Database saturation can limit throughput gains even when API CPU/request improves. '
    'API PSS under load is not a minimum allocation. This campaign uses fresh uncached '
    'controls; older uncached results are not substituted. Invalid/interrupted captures '
    'are excluded and retried in separately preserved campaign directories.','',
    '[Interactive charts](report/charts.html) · [Medians](medians.json) · [Provenance](provenance.json)','']
(output/'RESULTS.md').write_text('\n'.join(lines))
print(json.dumps(dict(medians=medians,comparisons=comparisons),indent=2))
