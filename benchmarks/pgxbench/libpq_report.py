"""Report standalone libpq CPU measurements separately from HTTP benchmarks."""
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

def report(measurements, output):
    groups = defaultdict(list)
    for row in measurements:
        if row.get('kind') != 'libpq_client_microbenchmark' or not row.get('passed') or row['requests'] <= 0:
            raise ValueError('invalid libpq microbenchmark evidence')
        groups[(row['profile'],row['database_tls'],row['body_bytes'],row['segments'],row['image_id'])].append(row)
    rows = []
    for (profile,tls,size,segments,image), cases in sorted(groups.items()):
        values=[r['cpu_ns_per_byte'] for r in cases]
        rows.append(dict(profile=profile,database_tls=tls,body_bytes=size,segments=segments,image_id=image,
            repetitions=len(cases),client_cpu_ns_per_byte_median=median(values),
            client_cpu_ns_per_byte_min=min(values),client_cpu_ns_per_byte_max=max(values),
            requests_per_second_median=median(r['requests_per_second'] for r in cases),
            client_cpu_percent_median=median(r['client_cpu_percent'] for r in cases)))
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/'medians.json').write_text(json.dumps(rows,indent=2)+'\n')
    if rows:
        with (output/'medians.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    text=['# Standalone libpq client CPU measurements','',
        'Prepared text-length SQL against stock PostgreSQL 17. Client process CPU excludes '
        'database CPU, pgxio, JWT and HTTP handling. Shared Colima VM; these rates are not API capacity. '
        'Compare matching body sizes, segment counts and database transports.','',
        '| Profile | DB TLS | Body bytes | Segments | Repetitions | Client CPU ns/byte | Range | Client CPU % | SQL requests/sec |',
        '|---|---|---:|---:|---:|---:|---|---:|---:|']
    for r in rows:
        text.append(f"| {r['profile']} | {r['database_tls']} | {r['body_bytes']} | {r['segments']} | {r['repetitions']} | "
            f"{r['client_cpu_ns_per_byte_median']:.4f} | {r['client_cpu_ns_per_byte_min']:.4f}–{r['client_cpu_ns_per_byte_max']:.4f} | "
            f"{r['client_cpu_percent_median']:.1f} | {r['requests_per_second_median']:.0f} |")
    (output/'RESULTS.md').write_text('\n'.join(text)+'\n')
    return rows
