/* Standalone borrowed-parameter contract tests; dedicated test database only. */
#include "postgres_fe.h"
#include "libpq-fe.h"
#include "libpq-int.h"
#include <poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#undef fprintf
#undef snprintf
#if defined(__linux__) || defined(__FreeBSD__)
extern void libpq_test_fail_alloc(long after) __attribute__((weak));
#endif

static void
require(int ok, PGconn *c, const char *what)
{
	if (!ok)
	{
		fprintf(stderr, "%s: %s\n", what, c ? PQerrorMessage(c) : "");
		exit(1);
	}
}
static double
now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}
static void
flush_all(PGconn *c)
{
	double		deadline = now() + 15;
	int			rc;

	while ((rc = PQflush(c)) == 1)
	{
		struct pollfd p = {PQsocket(c), POLLIN | POLLOUT, 0};

		require(now() < deadline, c, "flush deadline");
		require(poll(&p, 1, 1000) >= 0, c, "poll");
		if (p.revents & POLLIN)
			require(PQconsumeInput(c), c, "consume while flushing");
	}
	require(rc == 0, c, "flush");
}
static void
expect(PGconn *c, const char *value)
{
	PGresult   *r;

	flush_all(c);
	require(PQsetnonblocking(c, 0) == 0, c, "blocking mode");
	r = PQgetResult(c);
	require(r && PQresultStatus(r) == PGRES_TUPLES_OK && PQntuples(r) == 1 &&
			strcmp(PQgetvalue(r, 0, 0), value) == 0, c, "unexpected result");
	PQclear(r);
	require(PQgetResult(c) == NULL, c, "end of results");
}
static void
exec_ok(PGconn *c, const char *sql)
{
	PGresult   *r = PQexec(c, sql);

	require(r && (PQresultStatus(r) == PGRES_COMMAND_OK || PQresultStatus(r) == PGRES_TUPLES_OK), c, sql);
	PQclear(r);
}
static void
functional(PGconn *c)
{
	char		a[] = {'h', 'e'};	/* Intentionally no trailing NUL. */
	char		b[] = {'l', 'l', 'o'};
	PQbuffer	segs[] = {{a, sizeof(a)}, {NULL, 0}, {b, sizeof(b)}};
	PQborrowedParam p = {segs, 3, 0, 0};
	PGresult   *r;
	Oid			bytea_oid = 17;

	require(PQsendQueryParamsBorrowed(c, "SELECT $1::text", 1, NULL, &p, 0), c, "fragmented text");
	expect(c, "hello");
	r = PQprepare(c, "borrowed_test", "SELECT $1::text", 1, NULL);
	require(r && PQresultStatus(r) == PGRES_COMMAND_OK, c, "prepare");
	PQclear(r);
	require(PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "prepared text");
	expect(c, "hello");
	p.isnull = 1;
	require(PQsendQueryParamsBorrowed(c, "SELECT ($1::text IS NULL)::text", 1, NULL, &p, 0), c, "null");
	expect(c, "true");
	p.isnull = 0;
	p.nsegments = 0;
	require(PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "empty");
	expect(c, "");
	p.nsegments = 1;
	p.format = 1;
	segs[0].data = "\0\xff";
	segs[0].len = 2;
	require(PQsendQueryParamsBorrowed(c, "SELECT encode($1,'hex')", 1, &bytea_oid, &p, 0), c, "binary");
	expect(c, "00ff");
	{
		PQbuffer	label = {"label", 5};
		PQborrowedParam mixed[] = {p, {&label, 1, 0, 0}};
		Oid			types[] = {17, 25};

		require(PQsendQueryParamsBorrowed(c, "SELECT encode($1,'hex') || ':' || $2::text", 2, types, mixed, 0), c, "mixed parameter formats");
		expect(c, "00ff:label");
		mixed[1].isnull = 1;
		require(PQsendQueryParamsBorrowed(c, "SELECT encode($1,'hex') || ':' || coalesce($2::text,'null')", 2, types, mixed, 0), c, "mixed NULL");
		expect(c, "00ff:null");
	}
	require(PQsendQueryParamsBorrowed(c, "SELECT $1::bytea", 1, &bytea_oid, &p, 1), c, "binary result");
	flush_all(c);
	r = PQgetResult(c);
	require(r && PQresultStatus(r) == PGRES_TUPLES_OK && PQgetlength(r, 0, 0) == 2 && memcmp(PQgetvalue(r, 0, 0), "\0\xff", 2) == 0, c, "binary result bytes");
	PQclear(r);
	require(PQgetResult(c) == NULL, c, "binary result complete");
	p.format = 0;
	require(!PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "reject text NUL");
	segs[0].data = "x";
	p.nsegments = SIZE_MAX;
	require(!PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "reject descriptor count overflow");
	p.nsegments = 1;
	segs[0].len = (size_t) INT_MAX + 1;
	require(!PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "reject oversized parameter");
	segs[0].len = 1;
	segs[0].data = NULL;
	require(!PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "reject null pointer");
	segs[0].data = "x";
	p.format = 2;
	require(!PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "reject format");
	require(!PQsendQueryParamsBorrowed(c, "SELECT 1", -1, NULL, NULL, 0), c, "reject count");
	exec_ok(c, "SELECT 1");
	p.format = 0;
	p.nsegments = 1;
	{
		FILE	   *trace = tmpfile();

		require(trace != NULL, c, "trace file");
		PQtrace(c, trace);
		require(PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "trace fallback");
		expect(c, "x");
		PQuntrace(c);
		require(ftell(trace) > 0, c, "trace output");
		fclose(trace);
	}
}
static void
pipeline(PGconn *c)
{
	PQbuffer	s = {"hello", 5};
	PQborrowedParam p = {&s, 1, 0, 0};
	int			tuples = 0,
				fatal = 0,
				aborted = 0,
				sync = 0;

	require(PQenterPipelineMode(c), c, "enter pipeline");
	require(PQsendQueryParams(c, "SELECT 'legacy-before'", 0, NULL, NULL, NULL, NULL, 0), c, "legacy before");
	require(PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "pipeline prepared");
	require(PQsendQueryParams(c, "SELECT 'legacy-after'", 0, NULL, NULL, NULL, NULL, 0), c, "legacy after");
	require(PQsendQueryParamsBorrowed(c, "SELECT $1::text", 1, NULL, &p, 0), c, "pipeline params");
	require(PQsendQueryParams(c, "SELECT 1/0", 0, NULL, NULL, NULL, NULL, 0), c, "pipeline error");
	require(PQsendQueryPreparedBorrowed(c, "borrowed_test", 1, &p, 0), c, "pipeline skipped");
	require(PQpipelineSync(c), c, "sync");
	flush_all(c);
	while (!sync)
	{
		PGresult   *r = PQgetResult(c);

		if (!r)
			continue;
		switch (PQresultStatus(r))
		{
			case PGRES_TUPLES_OK:
				tuples++;
				break;
			case PGRES_FATAL_ERROR:
				fatal++;
				break;
			case PGRES_PIPELINE_ABORTED:
				aborted++;
				break;
			case PGRES_PIPELINE_SYNC:
				sync++;
				break;
			default:
				require(0, c, "pipeline status");
		}
		PQclear(r);
	}
	require(PQgetResult(c) == NULL && PQexitPipelineMode(c), c, "exit pipeline");
	require(tuples == 4 && fatal == 1 && aborted == 1, c, "pipeline ordering/counts");
	exec_ok(c, "SELECT 1");
}

/* Force genuine partial writes while the backend executes the first query. */
static void
mixed_partial(PGconn *c)
{
	const size_t bytes = 8 * 1024 * 1024;
	char	   *body = malloc(bytes + 1);
	const char *values[1];
	char		tailbytes[] = {'o', 'k'};
	PQbuffer	segment = {tailbytes, 2};
	PQborrowedParam p = {&segment, 1, 0, 0};
	int			sync = 0,
				index = 0,
				size = 1024;
	const char *expected[] = {"", "8388608", "ok", "legacy-tail"};

	require(body != NULL, c, "mixed allocation");
	memset(body, 'x', bytes);
	body[bytes] = 0;
	values[0] = body;
	setsockopt(PQsocket(c), SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
	require(PQsetnonblocking(c, 1) == 0 && PQenterPipelineMode(c), c, "mixed pipeline");
	require(PQsendQueryParams(c, "SELECT pg_sleep(0.5)", 0, NULL, NULL, NULL, NULL, 0), c, "mixed blocker");
	flush_all(c);
	require(PQsendQueryParams(c, "SELECT length($1::text)", 1, NULL, values, NULL, NULL, 0), c, "large legacy");
	memset(body, 0xa5, bytes);
	free(body);					/* Legacy input may be reclaimed immediately. */
	require(c->outCount > 0 && PQflush(c) == 1, c, "pending legacy write");
	require(PQsendQueryParamsBorrowed(c, "SELECT $1::text", 1, NULL, &p, 0), c, "borrow after legacy retry");
	require(PQsendQueryParams(c, "SELECT 'legacy-tail'", 0, NULL, NULL, NULL, NULL, 0), c, "mixed tail");
	require(PQpipelineSync(c), c, "mixed sync");
	flush_all(c);
	memset(tailbytes, 0xa5, sizeof(tailbytes));
	require(PQsetnonblocking(c, 0) == 0, c, "mixed blocking");
	while (!sync)
	{
		PGresult   *r = PQgetResult(c);

		if (!r)
			continue;
		if (PQresultStatus(r) == PGRES_PIPELINE_SYNC)
			sync = 1;
		else
		{
			require(PQresultStatus(r) == PGRES_TUPLES_OK && index < 4 && strcmp(PQgetvalue(r, 0, 0), expected[index++]) == 0, c, "mixed ordering");
		}
		PQclear(r);
	}
	require(index == 4 && PQgetResult(c) == NULL && PQexitPipelineMode(c), c, "mixed complete");
}

static void
pending(PGconn *c, const char *conninfo, int mode)
{
	const size_t bytes = 8 * 1024 * 1024,
				count = 256;
	char	   *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	PQbuffer   *segments = calloc(count, sizeof(*segments));
	PQborrowedParam p = {segments, count, 0, 0};
	size_t		i,
				borrowed = 0,
				owned = 0;
	int			size = 1024;
	PGBorrowedOutput *part;

	require(data != MAP_FAILED && segments, c, "allocate payload");
	memset(data, 'x', bytes);
	setsockopt(PQsocket(c), SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
	require(PQsetnonblocking(c, 1) == 0 && PQenterPipelineMode(c), c, "pending pipeline");
	require(PQsendQueryParams(c, "SELECT pg_sleep(0.5)", 0, NULL, NULL, NULL, NULL, 0), c, "backend blocker");
	flush_all(c);
	for (i = 0; i < count; i++)
	{
		segments[i].data = data + i * (bytes / count);
		segments[i].len = bytes / count;
	}
	require(PQsendQueryParamsBorrowed(c, "SELECT length($1::text)", 1, NULL, &p, 0), c, "large borrowed");
	memset(segments, 0xa5, count * sizeof(*segments));
	free(segments);				/* libpq owns descriptors. */
	require(PQflush(c) == 1, c, "forced pending flush");
	for (part = c->borrowHead; part; part = part->next)
	{
		if (part->owned)
			owned += part->len;
		else
		{
			require((uintptr_t) part->data >= (uintptr_t) data &&
					(uintptr_t) part->data + part->len <= (uintptr_t) data + bytes, c, "original payload pointer");
			borrowed += part->len - part->offset;
		}
	}
	require(borrowed > 65536 && owned < 4096, c, "payload is borrowed, metadata is bounded");
	if (mode == 0)
	{
		int			sync = 0,
					length_seen = 0;

		require(PQpipelineSync(c), c, "large sync");
		flush_all(c);
		require(!c->borrowHead, c, "queue fully released");
		require(mprotect(data, bytes, PROT_NONE) == 0, c, "poison flushed payload");
		require(PQsetnonblocking(c, 0) == 0, c, "large blocking mode");
		while (!sync)
		{
			PGresult   *r = PQgetResult(c);

			if (!r)
				continue;
			if (PQresultStatus(r) == PGRES_PIPELINE_SYNC)
				sync = 1;
			else
			{
				require(PQresultStatus(r) == PGRES_TUPLES_OK, c, "large result status");
				if (strcmp(PQgetvalue(r, 0, 0), "8388608") == 0)
					length_seen++;
			}
			PQclear(r);
		}
		require(length_seen == 1 && PQgetResult(c) == NULL && PQexitPipelineMode(c), c, "large result");
		PQfinish(c);
	}
	else if (mode == 1)
	{
		PQreset(c);
		require(PQstatus(c) == CONNECTION_OK && !c->borrowHead, c, "reset releases pending buffers");
		require(mprotect(data, bytes, PROT_NONE) == 0, c, "poison reset payload");
		require(PQsetnonblocking(c, 0) == 0, c, "reset blocking mode");
		exec_ok(c, "SELECT 1");
		PQfinish(c);
	}
	else if (mode == 2)
	{
		PQfinish(c);
		require(mprotect(data, bytes, PROT_NONE) == 0, NULL, "poison finished payload");
	}
	else
	{
		PGconn	   *killer = PQconnectdb(conninfo);
		char		sql[100];

		require(PQstatus(killer) == CONNECTION_OK, killer, "killer connection");
		snprintf(sql, sizeof(sql), "SELECT pg_terminate_backend(%d)", PQbackendPID(c));
		exec_ok(killer, sql);
		PQfinish(killer);
		/* PQflush errors alone do not permit reclaiming data. */
		PQflush(c);
		PQfinish(c);
		require(mprotect(data, bytes, PROT_NONE) == 0, NULL, "poison disconnected payload");
	}
	munmap(data, bytes);
}
static void
small_pipeline(PGconn *c)
{
	char		body[256];
	PQbuffer	s = {body, sizeof(body)};
	PQborrowedParam p = {&s, 1, 0, 0};
	int			i,
				tuples = 0,
				sync = 0;

	memset(body, 'x', sizeof(body));
	require(PQenterPipelineMode(c), c, "small pipeline");
	for (i = 0; i < 64; i++)
		require(PQsendQueryParamsBorrowed(c, "SELECT octet_length($1::text)", 1, NULL, &p, 0), c, "small queued query");
	require(c->borrowBytes < 8192, c, "small pipeline auto-flushes accumulated output");
	require(PQpipelineSync(c), c, "small sync");
	flush_all(c);
	require(!c->borrowHead && c->borrowBytes == 0, c, "small queue released");
	while (!sync)
	{
		PGresult   *r = PQgetResult(c);

		if (!r)
			continue;
		if (PQresultStatus(r) == PGRES_PIPELINE_SYNC)
			sync = 1;
		else
		{
			require(PQresultStatus(r) == PGRES_TUPLES_OK && strcmp(PQgetvalue(r, 0, 0), "256") == 0, c, "small result");
			tuples++;
		}
		PQclear(r);
	}
	require(tuples == 64 && PQgetResult(c) == NULL && PQexitPipelineMode(c), c, "small pipeline complete");
}

static void
allocation_failures(const char *conninfo)
{
#if defined(__linux__) || defined(__FreeBSD__)
	int			caught = 0;
	int			tracing,
				failure;

	if (!libpq_test_fail_alloc)
	{
		require(getenv("LIBPQ_TEST_SHORT_WRITES") == NULL, NULL, "allocation shim was loaded");
		return;
	}
	for (tracing = 0; tracing < 2; tracing++)
		for (failure = 0; failure < 32; failure++)
		{
			PGconn	   *c = PQconnectdb(conninfo);
			PQbuffer	segments[8];
			PQborrowedParam p = {segments, 8, 0, 0};
			FILE	   *trace = tracing ? tmpfile() : NULL;
			int			sent,
						sync = 0,
						tuples = 0,
						i;

			require(PQstatus(c) == CONNECTION_OK, c, "allocation test connection");
			if (trace)
				PQtrace(c, trace);
			for (i = 0; i < 8; i++)
			{
				segments[i].data = "abcdefgh" + i;
				segments[i].len = 1;
			}
			require(PQenterPipelineMode(c), c, "allocation pipeline");
			require(PQsendQueryParams(c, "SELECT 'prefix'", 0, NULL, NULL, NULL, NULL, 0), c, "allocation prefix");
			libpq_test_fail_alloc(failure);
			sent = PQsendQueryParamsBorrowed(c, "SELECT $1::text", 1, NULL, &p, 0);
			libpq_test_fail_alloc(-1);
			if (!sent)
			{
				caught++;
				require(!c->borrowHead, c, "failed submission retains no references");
			}
			if (PQstatus(c) == CONNECTION_OK)
			{
				require(PQpipelineSync(c), c, "allocation sync");
				flush_all(c);
				while (!sync)
				{
					PGresult   *r = PQgetResult(c);

					if (!r)
						continue;
					if (PQresultStatus(r) == PGRES_PIPELINE_SYNC)
						sync = 1;
					else
					{
						require(PQresultStatus(r) == PGRES_TUPLES_OK, c, "constructor failure did not publish partial message");
						tuples++;
					}
					PQclear(r);
				}
				require(tuples == 1 + sent && PQgetResult(c) == NULL && PQexitPipelineMode(c), c, "allocation result count");
			}
			if (trace)
			{
				PQuntrace(c);
				fclose(trace);
			}
			PQfinish(c);
		}
	require(caught >= 5, NULL, "allocation failure injection exercised constructors");
	fprintf(stderr, "allocation failures exercised: %d\n", caught);
#endif
}

int
main(int argc, char **argv)
{
	PGconn	   *c;
	int			i;

	require(argc >= 2, NULL, "connection string required");
	c = PQconnectdb(argv[1]);
	require(PQstatus(c) == CONNECTION_OK, c, "connect");
	functional(c);
	pipeline(c);
	small_pipeline(c);
	mixed_partial(c);
	PQfinish(c);
	for (i = 0; i < 4; i++)
	{
		c = PQconnectdb(argv[1]);
		require(PQstatus(c) == CONNECTION_OK, c, "pending connect");
		pending(c, argv[1], i);
	}
	allocation_failures(argv[1]);
	puts("{\"passed\":true,\"cases\":[\"parameters\",\"prepared\",\"binary\",\"null-empty\",\"validation\",\"trace\",\"mixed-pipeline-abort\",\"backpressure\",\"flush-release\",\"reset\",\"finish\",\"disconnect\"]}");
	return 0;
}
