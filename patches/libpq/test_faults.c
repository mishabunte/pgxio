/* Test-only syscall shim. Never linked or loaded by production builds. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

static unsigned calls;
static unsigned gate_calls[8192];

#if defined(__linux__) && !defined(PGXIO_IO_ONLY)
/* Legacy Linux allocator injection; portable diagnostics use test_alloc.c. */
extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_realloc(void *, size_t);
static long allocations = -1;
void		libpq_test_fail_alloc(long after);
void
libpq_test_fail_alloc(long after)
{
	allocations = after;
}
static int
fail_alloc(void)
{
	if (allocations < 0)
		return 0;
	if (allocations-- == 0)
	{
		allocations = -1;
		errno = ENOMEM;
		return 1;
	}
	return 0;
}
void *
malloc(size_t n)
{
	return fail_alloc() ? NULL : __libc_malloc(n);
}
void *
calloc(size_t n, size_t size)
{
	return fail_alloc() ? NULL : __libc_calloc(n, size);
}
void *
realloc(void *p, size_t n)
{
	return fail_alloc() ? NULL : __libc_realloc(p, n);
}
#endif
static int
fault(int fd)
{
	struct sockaddr_in peer;
	socklen_t	len = sizeof(peer);
	const char *gate = getenv("LIBPQ_TEST_BLOCK_FILE");

	if (getpeername(fd, (struct sockaddr *) &peer, &len) || peer.sin_family != AF_INET || ntohs(peer.sin_port) != 5432)
		return 0;
	if (gate && access(gate, F_OK) == 0)
	{
		if (getenv("LIBPQ_TEST_ALLOW_PREFIX") && fd >= 0 && fd < 8192 && gate_calls[fd]++ == 0)
			return 0;
		errno = EAGAIN;
		return 1;
	}
	if (fd >= 0 && fd < 8192)
		gate_calls[fd] = 0;
	if (!getenv("LIBPQ_TEST_SHORT_WRITES"))
		return 0;
	calls++;
	if (calls % 19 == 0)
	{
		errno = EINTR;
		return 1;
	}
	if (calls % 19 == 1)
	{
		errno = EAGAIN;
		return 1;
	}
	return 0;
}
ssize_t
send(int fd, const void *buf, size_t len, int flags)
{
	static ssize_t (*real_send) (int, const void *, size_t, int);

	if (!real_send)
		real_send = dlsym(RTLD_NEXT, "send");
	if (fault(fd))
		return -1;
	if (getenv("LIBPQ_TEST_SHORT_WRITES") && len > 16384)
		len = 16384;
	return real_send(fd, buf, len, flags);
}
ssize_t
sendmsg(int fd, const struct msghdr *message, int flags)
{
	static ssize_t (*real_sendmsg) (int, const struct msghdr *, int);
	struct msghdr msg = *message;
	struct iovec iov[16];
	size_t		used = 0,
				count = 0;

	if (!real_sendmsg)
		real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
	if (fault(fd))
		return -1;
	if (getenv("LIBPQ_TEST_ALLOW_PREFIX") && fd >= 0 && fd < 8192 && gate_calls[fd] == 1)
	{
		msg.msg_iovlen = message->msg_iovlen ? 1 : 0;
		return real_sendmsg(fd, &msg, flags);
	}
	if (!getenv("LIBPQ_TEST_SHORT_WRITES"))
		return real_sendmsg(fd, message, flags);
	while (count < (size_t) message->msg_iovlen && count < 16 && used < 16384)
	{
		iov[count] = message->msg_iov[count];
		if (iov[count].iov_len > 16384 - used)
			iov[count].iov_len = 16384 - used;
		used += iov[count].iov_len;
		count++;
	}
	msg.msg_iov = iov;
	msg.msg_iovlen = count;
	return real_sendmsg(fd, &msg, flags);
}
