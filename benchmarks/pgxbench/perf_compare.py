"""Stage comparisons, with product defaults and separate Nginx experiments."""
import random


def jobs(args):
    profiles=args.profiles.split(',')
    workloads=args.workloads.split(',')
    nginx_profiles=args.nginx_profiles.split(',')
    allowed={'baseline','recovery','prepared','pipeline','queue','cache','postgrest-uncached','postgrest-cached'}
    if not profiles or set(profiles)-allowed: raise ValueError('unknown performance stage')
    if not workloads or set(workloads)-{'one','page','write','mixed'}: raise ValueError('unknown REST workload')
    if set(nginx_profiles)-{'baseline','keepalive-10000','reuseport'}: raise ValueError('unknown Nginx profile')
    if args.repetitions<1 or args.warmup<0: raise ValueError('invalid repetition/warmup')
    delay=getattr(args,'database_delay_ms',0)
    if not 0 <= delay <= 1000: raise ValueError('database delay must be 0..1000 ms')
    result=[]
    for profile in profiles:
        native=profile.startswith('postgrest-')
        options={}
        if not native and profile!='baseline':
            options={'db_prepared_statements':profile!='recovery', 'db_pipeline':profile not in ('recovery','prepared'),
                     'pool_acquisition_timeout_ms':1000 if profile in ('queue','cache') else 0,
                     'pool_queue_limit':32, 'jwt_cache_max_entries':1000 if profile=='cache' else 0,
                     'jwt_cache_max_bytes':1048576}
        for nginx in (['baseline'] if native else nginx_profiles):
            for workload in workloads:
                for connections in args.connections:
                    for repetition in range(1,args.repetitions+1):
                        result.append(dict(profile=profile, target='postgrest' if native else 'pgxio',
                            pgxio_image='' if native else args.baseline_image if profile=='baseline' else args.image,
                            module_options=options, database_tls=getattr(args, 'database_tls', 'verify-full'), nginx_profile=nginx, database_delay_ms=delay, cpus=args.cpus, workers=args.workers or args.cpus,
                            auth_mode='jwt',jwt_cache='default' if profile=='postgrest-cached' else 'off',auth_fixture='public',
                            workload=workload,rate=0,sockets=0,subscriptions=1,active_tenants=10,probe_rate=1,realtime_mode='both',
                            engine='wrk',connections=connections,threads=2 if connections>1 and connections%2==0 else 1,
                            wrk_image=args.wrk_image,wrk_cpuset=args.wrk_cpuset,duration=args.duration,warmup=args.warmup,repetition=repetition))
    random.Random(args.seed).shuffle(result)
    for index,job in enumerate(result): job['id']=f'{index:03d}'
    return result
