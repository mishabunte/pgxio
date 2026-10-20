/* Appended to fe-exec.c only in a separate diagnostic build. */
#undef malloc
#undef calloc
#undef realloc
static long pgxio_test_allocations = -1;
void libpq_test_fail_alloc(long after);
void libpq_test_fail_alloc(long after) { pgxio_test_allocations = after; }
static int pgxio_test_fail(void) {
    if (pgxio_test_allocations < 0) return 0;
    if (pgxio_test_allocations-- == 0) {
        pgxio_test_allocations = -1; errno = ENOMEM; return 1;
    }
    return 0;
}
void *pgxio_test_malloc(size_t n) { return pgxio_test_fail() ? NULL : malloc(n); }
void *pgxio_test_calloc(size_t n, size_t s) { return pgxio_test_fail() ? NULL : calloc(n,s); }
void *pgxio_test_realloc(void *p, size_t n) { return pgxio_test_fail() ? NULL : realloc(p,n); }
