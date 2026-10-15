#!/usr/bin/env python3
"""Local smoke benchmark, NOT a capacity claim. Run on a quiet VPS for publishing.

Reports latency including failures, success throughput, cgroup CPU/memory, Nginx
PSS, and incremental PSS for idle WebSockets. No credentials appear in output.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import platform
import subprocess
import time
import httpx
from websockets.asyncio.client import connect

ROOT=Path(__file__).resolve().parent.parent

def command(*args):
    return subprocess.check_output(args,text=True,cwd=ROOT).strip()

def memory(container, process="nginx"):
    # All data is read inside the target container's namespaces. Ignore shell and
    # cat processes rather than counting measurement machinery as runtime memory.
    script='for d in /proc/[0-9]*; do read n < "$d/comm" || continue; if [ "$n" = "$1" ]; then cat "$d/smaps_rollup"; fi; done'
    identity=["--user","postgres"] if process=="postgres" else []
    raw=command("docker","exec",*identity,container,"sh","-c",script,"sh",process)
    result={"rss_kib":0,"pss_kib":0}
    for line in raw.splitlines():
        if line.startswith("Rss:"): result["rss_kib"]+=int(line.split()[1])
        if line.startswith("Pss:"): result["pss_kib"]+=int(line.split()[1])
    result["cgroup_bytes"]=int(command("docker","exec",container,"cat","/sys/fs/cgroup/memory.current"))
    cpu=command("docker","exec",container,"cat","/sys/fs/cgroup/cpu.stat")
    result["cpu_usec"]=int(dict(line.split() for line in cpu.splitlines())["usage_usec"])
    return result

def percentile(values, fraction):
    return sorted(values)[min(len(values)-1,math.ceil(len(values)*fraction)-1)] if values else None

async def main(args):
    token=command("python3","bin/pgxio","token")
    report={"kind":"development smoke; shared Colima host unless run elsewhere", "host":platform.platform(),"concurrency":args.concurrency,"requests":args.requests,"image":command("docker","image","inspect","pgxio:dev","--format","{{.Id}}"),"image_bytes":int(command("docker","image","inspect","pgxio:dev","--format","{{.Size}}"))}
    # Warm requests include schema/JWT caches before measuring steady-state PSS.
    headers={"Authorization":"Bearer "+token}
    async with httpx.AsyncClient(timeout=10,limits=httpx.Limits(max_connections=args.concurrency)) as client:
        for _ in range(20):
            response=await client.post(args.url+"/v1/list_tasks",json={},headers=headers)
            response.raise_for_status()
        before=memory(args.container)
        samples=[];statuses={};count=0
        async def worker():
            nonlocal count
            while count<args.requests:
                count+=1;start=time.perf_counter()
                try:
                    r=await client.post(args.url+"/v1/list_tasks",json={},headers=headers)
                    status=str(r.status_code)
                except httpx.HTTPError:
                    status="transport_error"
                samples.append((time.perf_counter()-start)*1000)
                statuses[status]=statuses.get(status,0)+1
        start=time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        elapsed=time.perf_counter()-start
        after=memory(args.container)
        report["rest"]={"seconds":elapsed,"successful_requests_per_second":statuses.get("200",0)/elapsed,"statuses":statuses,"p50_ms":percentile(samples,.5),"p95_ms":percentile(samples,.95),"p99_ms":percentile(samples,.99),"api_cpu_seconds":(after["cpu_usec"]-before["cpu_usec"])/1e6,"before":before,"after":after}
    sockets=[]
    try:
        for i in range(args.websockets):
            ws=await connect(args.url.replace("http://","ws://").replace("https://","wss://")+"/v1/events",subprotocols=["pgxio.v1"])
            await ws.send(json.dumps({"type":"auth","token":token}))
            assert json.loads(await ws.recv())["type"]=="ready"
            await ws.send(json.dumps({"type":"subscribe","id":"1","stream":"tasks","params":{}}))
            assert json.loads(await ws.recv())["type"]=="subscribed"
            sockets.append(ws)
        await asyncio.sleep(.2)
        active=memory(args.container)
        report["websockets"]={"count":len(sockets),"subscribed":active,"incremental_pss_bytes_per_socket":(active["pss_kib"]-after["pss_kib"])*1024/max(1,len(sockets))}
        if sockets:
            async with httpx.AsyncClient(timeout=10) as client:
                start=time.perf_counter()
                response=await client.post(args.url+"/v1/create_task",json={"title":"benchmark fanout"},headers=headers)
                response.raise_for_status()
                async def receive(ws):
                    event=json.loads(await asyncio.wait_for(ws.recv(),10))
                    assert event["type"]=="changed"
                    return (time.perf_counter()-start)*1000
                latency=await asyncio.gather(*(receive(ws) for ws in sockets))
                report["websockets"].update(fanout_p50_ms=percentile(latency,.5),fanout_p99_ms=percentile(latency,.99),fanout_includes="HTTP write + commit + notify + per-subscription SQL reauthorization")
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets))
    if args.baseline:
        report["plain_nginx_lower_bound"]=memory(args.baseline)
        report["module_incremental_warm_pss_kib"]=before["pss_kib"]-report["plain_nginx_lower_bound"]["pss_kib"]
    report["postgres"]=memory(args.postgres,"postgres")
    report["api_plus_postgres_pss_kib"]=after["pss_kib"]+report["postgres"]["pss_kib"]
    if args.measure_startup:
        times=[]
        for _ in range(3):
            start=time.perf_counter()
            await asyncio.to_thread(command,"docker","restart",args.container)
            async with httpx.AsyncClient(timeout=2) as client:
                for _ in range(100):
                    try:
                        if (await client.get(args.url+"/readyz")).status_code==200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError("API restart did not become ready")
            times.append((time.perf_counter()-start)*1000)
        report["cached_container_restart_to_ready_ms"]=times
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url",default="http://127.0.0.1:8080")
    p.add_argument("--container",default="pgxio-api-1")
    p.add_argument("--requests",type=int,default=1000)
    p.add_argument("--concurrency",type=int,default=4)
    p.add_argument("--websockets",type=int,default=100)
    p.add_argument("--baseline")
    p.add_argument("--postgres",default="pgxio-postgres-1")
    p.add_argument("--measure-startup",action="store_true",help="restart the local API three times after measuring")
    p.add_argument("--output",type=Path,default=Path("artifacts/smoke.json"))
    args=p.parse_args()
    if min(args.requests,args.concurrency)<1 or args.websockets<0: p.error("invalid workload size")
    asyncio.run(main(args))
