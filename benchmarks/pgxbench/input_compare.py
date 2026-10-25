"""Before/raw/schema input comparison using immutable runtime images."""
import random


def jobs(args):
    if args.repetitions < 1 or args.warmup < 0:
        raise ValueError('invalid repetitions or warmup')
    if any(size < 128 or size > 65536 for size in args.body_sizes):
        raise ValueError('request sizes must be 128..65536 bytes')
    result = []
    for profile, image, options in (
        ('input-before', args.baseline_image, {}),
        ('input-raw', args.image, {'validate_rest_input': False}),
        ('input-schema', args.image, {'validate_rest_input': True}),
    ):
        for size in args.body_sizes:
            for repetition in range(1, args.repetitions + 1):
                result.append(dict(profile=profile, target='pgxio', pgxio_image=image,
                    module_options=options, nginx_profile='baseline', cpus=1, workers=1,
                    auth_mode='jwt', jwt_cache='off', auth_fixture='public',
                    workload='one', request_bytes=size, rate=0, sockets=0, subscriptions=1,
                    active_tenants=10, probe_rate=1, realtime_mode='both', engine='wrk',
                    connections=args.connections, threads=min(2, args.connections),
                    wrk_image=args.wrk_image, wrk_cpuset=args.wrk_cpuset,
                    duration=args.duration, warmup=args.warmup, repetition=repetition))
    random.Random(args.seed).shuffle(result)
    for index, job in enumerate(result):
        job['id'] = f'{index:03d}'
    return result
