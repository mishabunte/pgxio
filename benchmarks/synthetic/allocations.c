/* Linux/glibc diagnostics only. Not loaded by timing binaries. */
#include <stddef.h>
#include <stdint.h>
extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t,size_t);
extern void *__libc_realloc(void *,size_t);
static _Thread_local int enabled;
static _Thread_local uint64_t calls, bytes;
void synth_alloc_begin(void) { calls=bytes=0; enabled=1; }
void synth_alloc_end(void) { enabled=0; }
uint64_t synth_alloc_calls(void) { return calls; }
uint64_t synth_alloc_bytes(void) { return bytes; }
void *malloc(size_t n) { if(enabled){calls++;bytes+=n;} return __libc_malloc(n); }
void *calloc(size_t n,size_t s) { if(enabled){calls++;bytes+=n*s;} return __libc_calloc(n,s); }
void *realloc(void *p,size_t n) { if(enabled){calls++;bytes+=n;} return __libc_realloc(p,n); }
