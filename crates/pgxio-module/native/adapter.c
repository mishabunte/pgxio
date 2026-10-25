/* Nginx owns HTTP/TLS sockets; libpq owns database sockets. Never close a
 * libpq descriptor here. Rust owns all application and protocol state. */
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>

extern ngx_module_t ngx_http_pgxio_module;
extern ngx_int_t pgx_start(ngx_http_request_t *r);
extern void pgx_body(ngx_http_request_t *r);
extern void pgx_cleanup(uint64_t id);
extern void pgx_socket(uint64_t id);
extern void pgx_database(size_t id);
extern void pgx_tick(void);
extern int pgx_init(const void *settings);
extern void *pgx_config_new(const void *directives, size_t count);
extern void pgx_config_free(void *settings);
extern void pgx_exit(void);

typedef struct { ngx_str_t name; size_t count; ngx_str_t *args; } pgx_directive;
typedef struct { ngx_array_t directives; void *settings; int enabled; } pgx_conf;
typedef struct { uint64_t id; } pgx_request;
typedef struct { ngx_connection_t *c; size_t id; } pgx_watch;
static ngx_event_t tick;

void *pgx_create_conf(ngx_conf_t *cf) {
    pgx_conf *c = ngx_pcalloc(cf->pool, sizeof(pgx_conf));
    if (!c || ngx_array_init(&c->directives, cf->pool, 8, sizeof(pgx_directive)) != NGX_OK) return NULL;
    return c;
}
static char *pgx_setting(ngx_conf_t *cf, ngx_command_t *cmd, void *conf) {
    pgx_conf *c = conf;
    pgx_directive *d = ngx_array_push(&c->directives);
    if (!d) return NGX_CONF_ERROR;
    d->name = cmd->name; d->count = cf->args->nelts - 1;
    d->args = ngx_palloc(cf->pool, d->count * sizeof(ngx_str_t));
    if (!d->args) return NGX_CONF_ERROR;
    ngx_memcpy(d->args, (ngx_str_t *)cf->args->elts + 1, d->count * sizeof(ngx_str_t));
    return NGX_CONF_OK;
}
char *pgx_init_conf(ngx_conf_t *cf, void *conf) {
    pgx_conf *c = conf;
    if (!c->enabled && !c->directives.nelts) return NGX_CONF_OK;
    c->settings = pgx_config_new(c->directives.elts, c->directives.nelts);
    if (!c->settings) return NGX_CONF_ERROR;
    ngx_pool_cleanup_t *cl = ngx_pool_cleanup_add(cf->pool, 0);
    if (!cl) { pgx_config_free(c->settings); c->settings = NULL; return NGX_CONF_ERROR; }
    cl->handler = pgx_config_free; cl->data = c->settings;
    return NGX_CONF_OK;
}
static char *pgx_enable(ngx_conf_t *cf, ngx_command_t *cmd, void *conf) {
    (void)cmd; (void)conf;
    ngx_http_core_loc_conf_t *lc = ngx_http_conf_get_module_loc_conf(cf, ngx_http_core_module);
    pgx_conf *c = ngx_http_conf_get_module_main_conf(cf, ngx_http_pgxio_module);
    c->enabled = 1;
    lc->handler = pgx_start;
    return NGX_CONF_OK;
}
#define PGX_SETTING(name, arity) { ngx_string("pgxio_" name), NGX_HTTP_MAIN_CONF|arity, pgx_setting, NGX_HTTP_MAIN_CONF_OFFSET, 0, NULL }
ngx_command_t pgx_commands[] = {
    PGX_SETTING("libpq_buf", NGX_CONF_TAKE1),
    PGX_SETTING("validate_input", NGX_CONF_TAKE1),
    PGX_SETTING("validate_output", NGX_CONF_TAKE1),
    PGX_SETTING("benchmark_no_jwt", NGX_CONF_TAKE1),
    PGX_SETTING("db_prepared_statements", NGX_CONF_TAKE1),
    PGX_SETTING("db_pipeline", NGX_CONF_TAKE1),
    PGX_SETTING("pool_acquisition_timeout_ms", NGX_CONF_TAKE1),
    PGX_SETTING("pool_queue_limit", NGX_CONF_TAKE1),
    PGX_SETTING("jwt_cache_max_entries", NGX_CONF_TAKE1),
    PGX_SETTING("jwt_cache_max_bytes", NGX_CONF_TAKE1),
    PGX_SETTING("contract", NGX_CONF_TAKE1),
    PGX_SETTING("issuer", NGX_CONF_TAKE1),
    PGX_SETTING("audience", NGX_CONF_TAKE1),
    PGX_SETTING("jwks_file", NGX_CONF_TAKE1),
    PGX_SETTING("development", NGX_CONF_TAKE1),
    PGX_SETTING("pool_size", NGX_CONF_TAKE1),
    PGX_SETTING("max_websockets", NGX_CONF_TAKE1),
    PGX_SETTING("origin", NGX_CONF_TAKE1),
    PGX_SETTING("endpoint", NGX_CONF_TAKE23),
    PGX_SETTING("stream", NGX_CONF_TAKE2),
    { ngx_string("pgxio"), NGX_HTTP_LOC_CONF|NGX_CONF_NOARGS, pgx_enable, 0, 0, NULL },
    ngx_null_command
};
static void timer(ngx_event_t *ev) {
    pgx_tick();
    if (!ngx_exiting && !ngx_quit && !ngx_terminate && !ev->timer_set) ngx_add_timer(ev, 1000);
}
void pgx_schedule_tick(uint64_t delay_ms) {
    if (!tick.handler || ngx_exiting || ngx_quit || ngx_terminate) return;
    ngx_msec_t delay=(ngx_msec_t)delay_ms;
    if (tick.timer_set) {
        if ((ngx_msec_int_t)(tick.timer.key - ngx_current_msec) <= (ngx_msec_int_t)delay) return;
        ngx_del_timer(&tick);
    }
    ngx_add_timer(&tick, delay);
}
ngx_int_t pgx_init_process(ngx_cycle_t *cycle) {
    pgx_conf *conf = ngx_http_cycle_get_module_main_conf(cycle, ngx_http_pgxio_module);
    if (!conf || !conf->settings) return NGX_OK;
    if (pgx_init(conf->settings) != 0) return NGX_ERROR;
    ngx_memzero(&tick, sizeof(tick));
    tick.handler = timer; tick.log = cycle->log; tick.cancelable = 1;
    ngx_add_timer(&tick, 1000);
    return NGX_OK;
}
void pgx_exit_process(ngx_cycle_t *cycle) {
    (void)cycle;
    if (tick.timer_set) ngx_del_timer(&tick);
    pgx_exit();
}
int pgx_stopping(void) { return ngx_exiting || ngx_quit || ngx_terminate; }
static void cleanup(void *data) { pgx_request *ctx=data; pgx_cleanup(ctx->id); }
int pgx_attach(ngx_http_request_t *r, uint64_t id) {
    ngx_http_cleanup_t *cl = ngx_http_cleanup_add(r, sizeof(pgx_request));
    if (!cl) return -1;
    pgx_request *ctx=cl->data; ctx->id=id; cl->handler=cleanup;
    ngx_http_set_ctx(r, ctx, ngx_http_pgxio_module);
    return 0;
}
uint64_t pgx_id(ngx_http_request_t *r) {
    pgx_request *ctx=ngx_http_get_module_ctx(r,ngx_http_pgxio_module);
    return ctx ? ctx->id : 0;
}
const unsigned char *pgx_uri(ngx_http_request_t *r, size_t *len) { *len=r->uri.len; return r->uri.data; }
const unsigned char *pgx_method(ngx_http_request_t *r, size_t *len) { *len=r->method_name.len; return r->method_name.data; }
int pgx_has_args(ngx_http_request_t *r) { return r->args.len != 0; }
int pgx_can_upgrade(ngx_http_request_t *r) {
    /* HTTP/2 streams share a connection: raw WebSocket framing is forbidden. */
    return r->http_version==NGX_HTTP_VERSION_11 && r==r->main
        && !r->headers_in.chunked && r->headers_in.content_length_n<=0;
}
const unsigned char *pgx_header(ngx_http_request_t *r, const char *name, size_t n, size_t *len) {
    ngx_list_part_t *part=&r->headers_in.headers.part;
    ngx_table_elt_t *h=part->elts;
    for (ngx_uint_t i=0;;i++) {
        if (i>=part->nelts) { if (!part->next) break; part=part->next; h=part->elts; i=0; }
        if (h[i].key.len==n && ngx_strncasecmp(h[i].key.data,(u_char *)name,n)==0) {
            *len=h[i].value.len; return h[i].value.data;
        }
    }
    *len=0; return NULL;
}
ngx_int_t pgx_read_body(ngx_http_request_t *r, int single) {
    r->request_body_in_single_buf=single;
    return ngx_http_read_client_request_body(r,pgx_body);
}
void pgx_wait_client(ngx_http_request_t *r) {
    r->read_event_handler=ngx_http_test_reading;
    ngx_handle_read_event(r->connection->read,0);
}
long pgx_body_length(ngx_http_request_t *r) {
    size_t n=0;
    if (!r->request_body) return 0;
    for (ngx_chain_t *c=r->request_body->bufs;c;c=c->next) {
        if (c->buf->in_file) return -1;
        size_t len=ngx_buf_in_memory(c->buf) ? (size_t)(c->buf->last-c->buf->pos) : 0;
        if (len>65536-n) return -2;
        n+=len;
    }
    return (long)n;
}
long pgx_copy_body(ngx_http_request_t *r, unsigned char *dst, size_t cap) {
    size_t n=0;
    if (!r->request_body) return 0;
    for (ngx_chain_t *c=r->request_body->bufs;c;c=c->next) {
        if (c->buf->in_file) return -1;
        size_t len=ngx_buf_in_memory(c->buf) ? (size_t)(c->buf->last-c->buf->pos) : 0;
        if (len>cap-n) return -1;
        if (len) ngx_memcpy(dst+n,c->buf->pos,len);
        n+=len;
    }
    return (long)n;
}
typedef struct { const unsigned char *data; size_t len; } pgx_segment;
long pgx_body_segments(ngx_http_request_t *r, pgx_segment *dst, size_t cap) {
    size_t count=0;
    if (!r->request_body) return 0;
    for (ngx_chain_t *c=r->request_body->bufs;c;c=c->next) {
        if (c->buf->in_file) return -1;
        size_t len=ngx_buf_in_memory(c->buf) ? (size_t)(c->buf->last-c->buf->pos) : 0;
        if (!len) continue;
        if (dst) {
            if (count>=cap) return -1;
            dst[count].data=c->buf->pos; dst[count].len=len;
        }
        count++;
    }
    return (long)count;
}
int pgx_add_header(ngx_http_request_t *r, const char *key, const unsigned char *val, size_t len) {
    ngx_table_elt_t *h=ngx_list_push(&r->headers_out.headers);
    if (!h) return -1;
    ngx_memzero(h,sizeof(*h));
    h->key.len=ngx_strlen(key); h->key.data=ngx_pnalloc(r->pool,h->key.len);
    h->value.data=ngx_pnalloc(r->pool,len);
    if (!h->key.data || !h->value.data) return -1;
    ngx_memcpy(h->key.data,key,h->key.len); ngx_memcpy(h->value.data,val,len);
    h->value.len=len; h->hash=1; return 0;
}
void pgx_reply(ngx_http_request_t *r, int status, const unsigned char *data, size_t len) {
    /* Early authentication/method failures still need to consume the body before
       Nginx can parse the next request on this keep-alive connection. */
    if (!r->request_body) {
        ngx_int_t discard = ngx_http_discard_request_body(r);
        if (discard != NGX_OK) { ngx_http_finalize_request(r, discard); return; }
    }
    ngx_buf_t *b=ngx_create_temp_buf(r->pool,len);
    if (!b) { ngx_http_finalize_request(r,NGX_HTTP_INTERNAL_SERVER_ERROR); return; }
    ngx_memcpy(b->pos,data,len); b->last=b->pos+len; b->last_buf=1;
    r->headers_out.status=status; r->headers_out.content_length_n=len;
    ngx_str_set(&r->headers_out.content_type,"application/json");
    ngx_int_t rc=ngx_http_send_header(r);
    if (rc==NGX_ERROR || rc>NGX_OK || r->header_only) { ngx_http_finalize_request(r,rc); return; }
    ngx_chain_t out={b,NULL};
    ngx_http_finalize_request(r,ngx_http_output_filter(r,&out));
}
static void ws_event(ngx_http_request_t *r) { pgx_socket(pgx_id(r)); }
int pgx_upgrade(ngx_http_request_t *r) {
    r->headers_out.status=NGX_HTTP_SWITCHING_PROTOCOLS;
    r->headers_out.content_length_n=-1;
    r->keepalive=0; r->header_only=1;
    if (ngx_http_send_header(r)==NGX_ERROR) return -1;
    r->read_event_handler=ws_event; r->write_event_handler=ws_event;
    if (r->connection->read->timer_set) ngx_del_timer(r->connection->read);
    ngx_handle_read_event(r->connection->read,0);
    return 0;
}
void pgx_hold(ngx_http_request_t *r) { r->main->count++; }
void pgx_release_hold(ngx_http_request_t *r) { r->main->count--; }
long pgx_preread(ngx_http_request_t *r,unsigned char *buf,size_t cap) {
    size_t n=r->header_in->last-r->header_in->pos;
    if (n>cap) return -1;
    ngx_memcpy(buf,r->header_in->pos,n); r->header_in->pos+=n; return n;
}
long pgx_recv(ngx_http_request_t *r,unsigned char *buf,size_t n) { return r->connection->recv(r->connection,buf,n); }
long pgx_send(ngx_http_request_t *r,const unsigned char *buf,size_t n) {
    if (r->connection->buffered || r->buffered) {
        if (ngx_http_output_filter(r,NULL)==NGX_ERROR) return NGX_ERROR;
        if (r->connection->buffered || r->buffered) return NGX_AGAIN;
    }
    return r->connection->send(r->connection,(u_char *)buf,n);
}
void pgx_ws_arm(ngx_http_request_t *r,int write,int again) {
    ngx_connection_t *c=r->connection;
    ngx_handle_read_event(c->read,0);
    if (write || c->buffered || r->buffered) ngx_handle_write_event(c->write,0);
    else if (c->write->active) ngx_del_event(c->write,NGX_WRITE_EVENT,0);
    if (again) { ngx_post_event(c->read,&ngx_posted_next_events); }
}
void pgx_close(ngx_http_request_t *r) {
    ngx_connection_t *c=r->connection;
    r->keepalive=0; ngx_http_finalize_request(r,NGX_ERROR);
    /* DB/timer callbacks are outside ngx_http_request_handler, which normally
     * drains this queue. Otherwise termination waits for another client event. */
    ngx_http_run_posted_requests(c);
}
static void db_event(ngx_event_t *ev) {
    ngx_connection_t *c=ev->data; pgx_watch *w=c->data;
    ev->ready=0; pgx_database(w->id);
}
void *pgx_watch_new(int fd,size_t id) {
    pgx_watch *w=ngx_alloc(sizeof(*w),ngx_cycle->log);
    if (!w) return NULL;
    w->c=ngx_get_connection(fd,ngx_cycle->log); w->id=id;
    if (!w->c) { ngx_free(w); return NULL; }
    w->c->data=w; w->c->read->handler=db_event; w->c->write->handler=db_event;
    w->c->read->log=ngx_cycle->log; w->c->write->log=ngx_cycle->log;
    return w;
}
int pgx_watch_arm(void *ptr,int read,int write) {
    pgx_watch *w=ptr; ngx_connection_t *c=w->c;
    if (read && ngx_handle_read_event(c->read,0)!=NGX_OK) return -1;
    if (write && ngx_handle_write_event(c->write,0)!=NGX_OK) return -1;
    if (!read && c->read->active) ngx_del_event(c->read,NGX_READ_EVENT,0);
    if (!write && c->write->active) ngx_del_event(c->write,NGX_WRITE_EVENT,0);
    return 0;
}
void pgx_watch_free(void *ptr) {
    if (!ptr) return;
    pgx_watch *w=ptr; ngx_connection_t *c=w->c;
    /* PQconsumeInput may already have closed a failed socket. CLOSE_EVENT
     * clears Nginx state without epoll_ctl on a possibly reused descriptor. */
    if (ngx_del_conn) { ngx_del_conn(c,NGX_CLOSE_EVENT); }
    else {
        if (c->read->active) { ngx_del_event(c->read,NGX_READ_EVENT,NGX_CLOSE_EVENT); }
        if (c->write->active) { ngx_del_event(c->write,NGX_WRITE_EVENT,NGX_CLOSE_EVENT); }
    }
    /* These Nginx macros expand to multiple statements: braces are mandatory. */
    if (c->read->posted) { ngx_delete_posted_event(c->read); }
    if (c->write->posted) { ngx_delete_posted_event(c->write); }
    ngx_free_connection(c); c->fd=(ngx_socket_t)-1; ngx_free(w);
}
