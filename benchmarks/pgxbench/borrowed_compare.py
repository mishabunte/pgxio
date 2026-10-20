"""Matched upstream-libpq control, patched copy, and borrowed REST input."""
import random

def jobs(args):
    if args.repetitions < 1 or args.warmup < 0:
        raise ValueError('invalid repetitions/warmup')
    if any(n < 128 or n > 65536 for n in args.body_sizes):
        raise ValueError('body sizes must be 128..65536')
    if any(n not in (1, 8, 32) for n in args.connections):
        raise ValueError('connections must be 1, 8, or 32')
    transports = args.database_tls.split(',')
    if not transports or set(transports) - {'disable', 'verify-full'}:
        raise ValueError('database TLS must be disable and/or verify-full')
    result = []
    for profile, image, options in (
        ('libpq-control', args.unpatched_image, {}),
        ('libpq-patched-copy', args.image, {'libpq_buf': 'copy'}),
        ('libpq-borrowed', args.image, {'libpq_buf': 'borrowed'}),
    ):
        for tls in transports:
            for size in args.body_sizes:
                for connections in args.connections:
                    for repetition in range(1, args.repetitions + 1):
                        result.append(dict(profile=profile,target='pgxio',pgxio_image=image,
                            module_options=options,database_tls=tls,nginx_profile='baseline',cpus=1,workers=1,
                            auth_mode='jwt',jwt_cache='off',auth_fixture='public',workload='one',
                            request_bytes=size,rate=0,sockets=0,subscriptions=1,active_tenants=10,
                            probe_rate=1,realtime_mode='both',engine='wrk',connections=connections,
                            threads=min(2,connections),wrk_image=args.wrk_image,wrk_cpuset=args.wrk_cpuset,
                            duration=args.duration,warmup=args.warmup,repetition=repetition))
    random.Random(args.seed).shuffle(result)
    for index, job in enumerate(result): job['id'] = f'{index:03d}'
    return result
