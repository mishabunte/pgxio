/* Diagnostic build only: inject failures into borrowed constructors on any OS. */
#include <stdlib.h>
extern void *pgxio_test_malloc(size_t);
extern void *pgxio_test_calloc(size_t, size_t);
extern void *pgxio_test_realloc(void *, size_t);
#define malloc pgxio_test_malloc
#define calloc pgxio_test_calloc
#define realloc pgxio_test_realloc
