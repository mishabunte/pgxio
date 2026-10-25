"""Evaluate the three-percent copy/auto default gate, retaining every valid trial."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median
from pgxbench.report import make_report

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('sources',nargs='+',type=Path)
p.add_argument('--output',type=Path,default=Path('artifacts/buffer-compare-summary'))
args=p.parse_args();output=args.output;output.mkdir(parents=True,exist_ok=True)
selected={};trials=output/'trials';trials.mkdir(exist_ok=True)
for source in args.sources:
    env=json.loads((source/'environment.json').read_text());assert 'finished_unix' in env
    for r in json.loads((source/'outcomes.json').read_text()):
        if not r['passed'] or any(k!='http_200' and v for k,v in r['counts'].items()):continue
        j=r['job'];assert j['id'] not in selected
        selected[j['id']]=r
        stem=f"{j['profile']}-{j['id']}-{j['target']}-{j['auth_mode']}-{j['jwt_cache']}-{j['connections']}c-r{j['repetition']}"
        for suffix in ('.json','.resources.jsonl'):
            original=(source/'results'/(stem+suffix)).resolve();assert original.exists()
            target=trials/(source.name+'-'+original.name)
            if not target.exists():target.symlink_to(original)
make_report(trials,output/'report')
rows=json.loads((output/'report/summary.json').read_text())['rows']
assert all(r['workload_passed'] and r['resource_evidence_valid'] and r['HTTP_error_fraction']==0 for r in rows)
groups=defaultdict(list)
for r in rows:groups[r['database_tls'],r['request_bytes'],r['performance_profile']].append(r)
assert len(groups)==12 and all(len(g) in (5,7) for g in groups.values()),'incomplete campaign'
comparisons=[]
for tls in ('disable','verify-full'):
    for size in (1024,16384,65536):
        c=groups[tls,size,'buffer-copy'];a=groups[tls,size,'buffer-auto'];assert len(c)==len(a)
        c_rps=median(r['success_rps'] for r in c);a_rps=median(r['success_rps'] for r in a)
        c_cpu=median(r['api_cpu_ms_per_success'] for r in c);a_cpu=median(r['api_cpu_ms_per_success'] for r in a)
        comparisons.append(dict(database_tls=tls,body_bytes=size,repetitions=len(c),copy_rps=c_rps,auto_rps=a_rps,
            copy_cpu_ms=c_cpu,auto_cpu_ms=a_cpu,rps_ratio=a_rps/c_rps,cpu_ratio=a_cpu/c_cpu,
            passed=a_rps/c_rps>=.97 and a_cpu/c_cpu<=1.03,
            copy_rps_range=[min(r['success_rps'] for r in c),max(r['success_rps'] for r in c)],
            auto_rps_range=[min(r['success_rps'] for r in a),max(r['success_rps'] for r in a)]))
passed=all(r['passed'] for r in comparisons)
result=dict(passed=passed,comparisons=comparisons,rule='auto/copy RPS >=0.97 and CPU/request <=1.03, zero HTTP errors and valid resources',
            recommendation='auto default eligible' if passed else ('retain copy default; seven-repetition gate failed' if all(r['repetitions']==7 for r in comparisons) else 'retain copy default; add repetitions through seven before a final decision'))
(output/'gate.json').write_text(json.dumps(result,indent=2)+'\n')
lines=['# Copy versus auto acceptance gate','',
    f"Gate passed: **{passed}**. Same frozen module image; one API core, four database cores, HTTP frontend, RS256 JWT cache off, pool eight, 32 connections. "
    'Same one-row RPC for every body size, 10-second warmup and 30-second measurement, randomized paired repetitions. '
    'Effective input mode was asserted using test-build-only counters before every trial.','',
    '| DB TLS | Input KiB | Reps / mode | Copy RPS | Auto RPS | RPS change | CPU/request change | Gate |',
    '|---|---:|---:|---:|---:|---:|---:|---|']
for r in comparisons:lines.append(f"| {r['database_tls']} | {r['body_bytes']//1024} | {r['repetitions']} | {r['copy_rps']:.0f} | {r['auto_rps']:.0f} | {(r['rps_ratio']-1)*100:+.1f}% | {(r['cpu_ratio']-1)*100:+.1f}% | {r['passed']} |")
lines+=['','A pass allows at most 3% median throughput regression and at most 3% median API CPU/request regression in every tested case. '
    'Shared-host noise and overlapping ranges limit inference; this is a default-release gate, not a universal speed guarantee. '
    'Invalid captures and HTTP-error trials are excluded and must be replaced.','',
    '[Charts](report/charts.html) · [Raw gate data and ranges](gate.json)','']
(output/'RESULTS.md').write_text('\n'.join(lines))
print(json.dumps(result,indent=2))
