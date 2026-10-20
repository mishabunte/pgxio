"""Compare validation modes with identical fixtures/authentication and resources."""
import random


def jobs(args):
    if args.repetitions < 1 or args.warmup < 0 or any(c < 1 for c in args.connections):
        raise ValueError('invalid benchmark repetitions, warmup or connections')
    workloads = args.workloads.split(',')
    if any(w not in ('one','page') for w in workloads):
        raise ValueError('validation comparison supports one/page reads')
    profiles = [
        ('baseline', args.baseline_image, True, {}, 'legacy-json'),
        ('raw', args.image, False, {'validate_rest_input':False,'validate_output':False}, 'nginx-v1'),
        ('metadata', args.image, True, {'validate_rest_input':False,'validate_output':False}, 'nginx-v1'),
        ('output-schema', args.image, True, {'validate_rest_input':False,'validate_output':True}, 'nginx-v1'),
        ('both-schemas', args.image, True, {'validate_rest_input':True,'validate_output':True}, 'nginx-v1'),
    ]
    result=[]
    for profile,image,contract,options,format in profiles:
        for workload in workloads:
            for connections in args.connections:
                for repetition in range(1,args.repetitions+1):
                    result.append(dict(profile=profile,pgxio_image=image,target='pgxio',contract_enabled=contract,
                        config_format=format,module_options=options,workload=workload,connections=connections,
                        auth_mode='jwt',jwt_cache='off',auth_fixture='public',cpus=1,workers=1,
                        database_tls='verify-full',rate=0,sockets=0,subscriptions=1,active_tenants=10,
                        probe_rate=1,realtime_mode='both',engine='wrk',threads=2 if connections%2==0 else 1,
                        wrk_image=args.wrk_image,wrk_cpuset=args.wrk_cpuset,
                        duration=args.duration,warmup=args.warmup,repetition=repetition))
    random.Random(args.seed).shuffle(result)
    for i,job in enumerate(result): job['id']=f'{i:03d}'
    return result
