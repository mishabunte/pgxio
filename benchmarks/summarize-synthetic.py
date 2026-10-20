"""Summarize socket-free timings and separately collected diagnostics."""
import argparse
from collections import defaultdict
import csv
import html
import json
from pathlib import Path
from statistics import median

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('directory',type=Path)
args=p.parse_args();directory=args.directory
environment=json.loads((directory/'environment.json').read_text())
assert 'finished_unix' in environment,'campaign still running'
timing=[json.loads(l) for l in (directory/'timing.jsonl').read_text().splitlines()]
diagnostics=[json.loads(l) for l in (directory/'diagnostics.jsonl').read_text().splitlines()]
def key(r):return tuple(r[k] for k in ('stage','transport','body_bytes','segments','mode'))
groups=defaultdict(list)
for row in timing:
    assert row['validated'] and not row['diagnostics'] and row['socket_operations']==0
    groups[key(row)].append(row)
diag={key(row):row for row in diagnostics}
expected=1 if environment['smoke'] else 5
assert set(groups)==set(diag) and all(len(g)==expected for g in groups.values())
plan=json.loads((directory/'plan.json').read_text())
assert len(timing)==len(plan['timing']) and len(diagnostics)==len(plan['diagnostics'])
summaries=[]
for k,g in sorted(groups.items()):
    values=sorted(r['cpu_ns_per_op'] for r in g)
    r=dict(zip(('stage','transport','body_bytes','segments','mode'),k))
    r['effective_mode']=diag[k].get('effective_mode',r['mode'])
    r.update(repetitions=len(g),cpu_ns_per_op=median(values),wall_ns_per_op=median(v['wall_ns_per_op'] for v in g),
             cpu_ns_per_byte=median(v['cpu_ns_per_byte'] for v in g),cpu_ns_min=values[0],cpu_ns_max=values[-1],
             cpu_ns_q1=values[len(values)//4],cpu_ns_q3=values[3*len(values)//4])
    r.update({field:value for field,value in diag[k].items() if field.endswith('_per_op') and field not in ('cpu_ns_per_op','wall_ns_per_op')})
    summaries.append(r)
index={key(r):r for r in summaries};comparisons=[]
for k in sorted({key(r)[:-1] for r in summaries}):
    c=index[(*k,'copy')];b=index[(*k,'borrowed')]
    comparisons.append(dict(zip(('stage','transport','body_bytes','segments'),k))|
        dict(copy_ns=c['cpu_ns_per_op'],borrowed_ns=b['cpu_ns_per_op'],
             borrowed_over_copy=b['cpu_ns_per_op']/c['cpu_ns_per_op'],
             ranges_overlap=not(b['cpu_ns_max']<c['cpu_ns_min'] or c['cpu_ns_max']<b['cpu_ns_min'])))
(directory/'summary.json').write_text(json.dumps(dict(rows=summaries,comparisons=comparisons),indent=2)+'\n')
with (directory/'summary.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=list(summaries[0]));writer.writeheader();writer.writerows(summaries)
lines=['# Socket-free borrowed versus copy','',
    f"{len(timing)} timing trials and {len(diagnostics)} separate diagnostic cases passed; {expected} repetitions per timing case. "
    'CPU nanoseconds per operation are medians. Allocator diagnostics report requested allocation bytes, not RSS. '
    'Borrowed/copy ratios below 1 favor borrowing; above 1 favor copying.','',
    'Uses production Rust input and borrowed-send code, static patched libpq, pre-existing separate input chunks, '
    'and prepared query messages. Setup, TLS handshake, peer decryption and correctness checks are outside timing. '
    'No sockets, PostgreSQL, HTTP, JWT or server results. One operation per complete flush, without batching or backpressure. '
    'Explicit versus default PostgreSQL text-format headers are checked for semantic equivalence.','']
if ('combined','plain',1024,1,'copy') in index:
    lines+=['## Representative results','',
        '| Input | Segments | Transport | Copy µs/op | Borrowed µs/op | Borrowed / copy |',
        '|---|---:|---|---:|---:|---:|']
    for r in comparisons:
        if r['stage']=='combined' and r['body_bytes'] in (1024,65536) and r['segments'] in (1,16):
            lines.append(f"| {r['body_bytes']//1024} KiB | {r['segments']} | {r['transport']} | "
                         f"{r['copy_ns']/1000:.3f} | {r['borrowed_ns']/1000:.3f} | {r['borrowed_over_copy']:.2f}× |")
    c=index['input','none',1024,1,'copy'];b=index['input','none',1024,1,'borrowed']
    pc=index['combined','plain',1024,1,'copy'];pb=index['combined','plain',1024,1,'borrowed']
    tc=index['combined','tls',1024,1,'copy'];tb=index['combined','tls',1024,1,'borrowed']
    lines+=['',f"For a contiguous 1 KiB body, Rust input construction alone costs {c['cpu_ns_per_op']:.1f} ns copied "
        f"versus {b['cpu_ns_per_op']:.1f} ns borrowed. Combined plaintext allocation traffic is "
        f"{pc['allocation_calls_per_op']:.0f} calls/op copied versus {pb['allocation_calls_per_op']:.0f} borrowed. "
        f"TLS emits {tc['tls_records_per_op']:.0f} versus {tb['tls_records_per_op']:.0f} records/op, "
        f"and {tc['ciphertext_bytes_per_op']:.0f} versus {tb['ciphertext_bytes_per_op']:.0f} encrypted bytes. "
        'This separates the input-copy saving from descriptor/protocol allocation and TLS fragmentation costs.','',
        'The same matrix includes larger bodies where avoided copying may outweigh metadata overhead. '
        'Read the transport and segment count together; no single size threshold is inferred for production.','']
lines+=[
    '## All comparisons','',
    '| Stage | Transport | Input B | Segments | Copy ns/op | Borrowed ns/op | Borrowed / copy | Ranges overlap |',
    '|---|---|---:|---:|---:|---:|---:|---|']
for r in comparisons:
    lines.append(f"| {r['stage']} | {r['transport']} | {r['body_bytes']} | {r['segments']} | {r['copy_ns']:.1f} | {r['borrowed_ns']:.1f} | {r['borrowed_over_copy']:.2f}× | {r['ranges_overlap']} |")
lines+=['','## Separate diagnostics','',
    '| Stage | Transport | Input B | Segments | Mode | Allocations/op | Allocated B/op | Sink calls/op | TLS writes/op | TLS records/op | Ciphertext B/op |',
    '|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|']
for r in summaries:
    lines.append(f"| {r['stage']} | {r['transport']} | {r['body_bytes']} | {r['segments']} | {r['mode']} | "
        +' | '.join(f"{r[k]:.1f}" for k in ('allocation_calls_per_op','allocated_bytes_per_op','sink_calls_per_op',
                    'tls_write_calls_per_op','tls_records_per_op','ciphertext_bytes_per_op'))+' |')
auto=[r for r in summaries if r['mode']=='auto']
if auto:
    lines+=['','## Auto versus selected explicit path','',
        '| Stage | Transport | Input B | Segments | Selected | Auto ns/op | Explicit ns/op | Auto / explicit |',
        '|---|---|---:|---:|---|---:|---:|---:|']
    for r in auto:
        reference=index[(*key(r)[:-1],r['effective_mode'])]
        lines.append(f"| {r['stage']} | {r['transport']} | {r['body_bytes']} | {r['segments']} | {r['effective_mode']} | "
            f"{r['cpu_ns_per_op']:.1f} | {reference['cpu_ns_per_op']:.1f} | {r['cpu_ns_per_op']/reference['cpu_ns_per_op']:.2f}× |")
lines+=['','These isolated costs do not establish HTTP throughput or a production crossover threshold. '
    'The in-memory sink excludes kernel/network costs; it retains real protocol construction and TLS encryption. '
    'Shared-host scheduling and CPU frequency can affect timing; ranges and quartiles are in summary.json. '
    'No instrumented timing is used in these comparisons.','',
    '[Interactive charts](charts.html) · [Raw summary](summary.json) · [Environment and sources](environment.json)','']
(directory/'RESULTS.md').write_text('\n'.join(lines))
data=json.dumps(comparisons).replace('<','\\u003c')
page='''<!doctype html><meta charset="utf-8"><title>Borrowed vs copy — synthetic</title>
<style>body{font:16px system-ui;max-width:1050px;margin:30px auto;padding:0 20px;color:#172033}select{font:inherit;margin:8px}svg{width:100%;height:auto}table{border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}.legend{color:#475569}</style>
<h1>Borrowed vs copy: no sockets or database</h1><p>Median process CPU time; five repetitions. Lower is faster. Timings exclude allocation instrumentation.</p>
<label>Stage <select id="stage"><option>combined</option><option>libpq</option><option>input</option></select></label>
<label>Transport <select id="transport"><option>plain</option><option>tls</option><option>none</option></select></label>
<label>Segments <select id="segments"><option>1</option><option>4</option><option>16</option></select></label>
<p class="legend">Blue: copy · Orange: borrowed</p><svg id="chart" viewBox="0 0 1000 420"></svg><table id="table"></table>
<p><a href="RESULTS.md">Full report with allocation and TLS-write diagnostics</a> · <a href="summary.json">Ranges and quartiles</a></p>
<script>const data=DATA;const ns='http://www.w3.org/2000/svg';function draw(){const rows=data.filter(r=>r.stage===stage.value&&r.transport===transport.value&&r.segments===+segments.value);chart.replaceChildren();const max=Math.max(1,...rows.flatMap(r=>[r.copy_ns,r.borrowed_ns]));function el(tag,a,text){let e=document.createElementNS(ns,tag);for(let k in a)e.setAttribute(k,a[k]);if(text)e.textContent=text;chart.appendChild(e)}rows.forEach((r,i)=>{let y=20+i*75;el('text',{x:0,y:y+20},r.body_bytes+' B');for(let [value,color,j]of[[r.copy_ns,'#2563eb',0],[r.borrowed_ns,'#ea580c',1]]){el('rect',{x:100,y:y+j*23,width:value/max*650,height:18,fill:color});el('text',{x:110+value/max*650,y:y+14+j*23},value.toFixed(1)+' ns')}});table.innerHTML='<tr><th>Input</th><th>Copy ns/op</th><th>Borrowed ns/op</th><th>Borrowed / copy</th></tr>'+rows.map(r=>`<tr><td>${r.body_bytes}</td><td>${r.copy_ns.toFixed(1)}</td><td>${r.borrowed_ns.toFixed(1)}</td><td>${r.borrowed_over_copy.toFixed(2)}×</td></tr>`).join('')}for(let id of['stage','transport','segments'])document.getElementById(id).onchange=()=>{if(stage.value==='input')transport.value='none';else if(transport.value==='none')transport.value='plain';draw()};draw();</script>'''
(directory/'charts.html').write_text(page.replace('DATA',data).replace('five repetitions',f'{expected} repetitions'))
print(json.dumps(dict(timing_trials=len(timing),diagnostic_cases=len(diagnostics),report=str(directory/'RESULTS.md'))))
