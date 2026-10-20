/* Standalone libpq client CPU benchmark; deliberately no JSON parsing/JWT. */
#include <libpq-fe.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double
clock_seconds(clockid_t id)
{
	struct timespec t;

	clock_gettime(id, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}
static void
check(int ok, PGconn *c)
{
	if (!ok)
	{
		fprintf(stderr, "libpq benchmark: %s\n", PQerrorMessage(c));
		exit(1);
	}
}
int
main(int argc, char **argv)
{
	PGconn	   *c;
	PGresult   *r;
	char	   *body;
	size_t		bytes,
				count,
				requests = 0,
				i;
	int			borrowed;
	double		duration,
				warmup,
				start,
				cpu,
				elapsed,
				cpu_elapsed;
#ifdef LIBPQ_HAS_BORROWED_PARAMS
	PQbuffer   *segments;
	PQborrowedParam param;
#endif
	if (argc != 7)
		return 2;
	bytes = strtoull(argv[2], NULL, 10);
	count = strtoull(argv[3], NULL, 10);
	duration = strtod(argv[4], NULL);
	warmup = strtod(argv[5], NULL);
	borrowed = strcmp(argv[6], "borrowed") == 0;
	if (!bytes || bytes > 1048576 || !count || count > bytes || duration <= 0 || warmup < 0)
		return 2;
	c = PQconnectdb(argv[1]);
	check(PQstatus(c) == CONNECTION_OK, c);
	body = malloc(bytes + 1);
	check(body != NULL, c);
	memset(body, 'x', bytes);
	body[bytes] = 0;
#ifdef LIBPQ_HAS_BORROWED_PARAMS
	segments = calloc(count, sizeof(*segments));
	check(segments != NULL, c);
	for (i = 0; i < count; i++)
	{
		size_t		begin = i * bytes / count,
					end = (i + 1) * bytes / count;

		segments[i].data = body + begin;
		segments[i].len = end - begin;
	}
	param.segments = segments;
	param.nsegments = count;
	param.format = 0;
	param.isnull = 0;
#else
	if (borrowed)
		return 2;
	(void) i;
#endif
	r = PQprepare(c, "bench", "SELECT octet_length($1::text)", 1, NULL);
	check(r && PQresultStatus(r) == PGRES_COMMAND_OK, c);
	PQclear(r);
	start = clock_seconds(CLOCK_MONOTONIC);
	cpu = clock_seconds(CLOCK_PROCESS_CPUTIME_ID);
	/* Warmup and measurement use the identical request path. */
	for (int phase = 0; phase < 2; phase++)
	{
		double		end = clock_seconds(CLOCK_MONOTONIC) + (phase ? duration : warmup);

		if (phase)
		{
			start = clock_seconds(CLOCK_MONOTONIC);
			cpu = clock_seconds(CLOCK_PROCESS_CPUTIME_ID);
			requests = 0;
		}
		while (clock_seconds(CLOCK_MONOTONIC) < end)
		{
#ifdef LIBPQ_HAS_BORROWED_PARAMS
			if (borrowed)
				check(PQsendQueryPreparedBorrowed(c, "bench", 1, &param, 0), c);
			else
#endif
			{
				const char *values[] = {body};

				check(PQsendQueryPrepared(c, "bench", 1, values, NULL, NULL, 0), c);
			}
			r = PQgetResult(c);
			check(r && PQresultStatus(r) == PGRES_TUPLES_OK && strtoull(PQgetvalue(r, 0, 0), NULL, 10) == bytes, c);
			PQclear(r);
			check(PQgetResult(c) == NULL, c);
			requests++;
		}
	}
	elapsed = clock_seconds(CLOCK_MONOTONIC) - start;
	cpu_elapsed = clock_seconds(CLOCK_PROCESS_CPUTIME_ID) - cpu;
	printf("{\"kind\":\"libpq_client_microbenchmark\",\"requests\":%zu,\"body_bytes\":%zu,\"segments\":%zu,\"borrowed\":%s,\"libpq_version\":%d,\"ssl\":%s,\"wall_seconds\":%.9f,\"client_cpu_seconds\":%.9f,\"client_cpu_percent\":%.3f,\"cpu_ns_per_byte\":%.6f,\"requests_per_second\":%.3f}\n",
		   requests, bytes, count, borrowed ? "true" : "false", PQlibVersion(), PQsslInUse(c) ? "true" : "false", elapsed, cpu_elapsed,
		   100 * cpu_elapsed / elapsed, 1e9 * cpu_elapsed / (bytes * requests), requests / elapsed);
	PQfinish(c);
#ifdef LIBPQ_HAS_BORROWED_PARAMS
	free(segments);
#endif
	free(body);
	return 0;
}
