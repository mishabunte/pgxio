/* Test-only fixture: real libpq serialization and OpenSSL, no socket I/O. */
#include "postgres_fe.h"
#include "libpq-int.h"
#include <openssl/ssl.h>
#include <openssl/ec.h>
#include <openssl/err.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <dlfcn.h>

#define FAKE_FD 123456789
#define CAPTURE_SIZE (256 * 1024)
typedef struct {
    PGconn *conn;
    SSL *peer;
    unsigned char captured[CAPTURE_SIZE];
    unsigned char decoded[CAPTURE_SIZE];
    size_t captured_len, decoded_len;
    int capture;
} Fixture;
static Fixture *active;
static uint64_t writes, tls_writes, records, cipher_bytes;
static SSL_CTX *client_ctx, *server_ctx;
static BIO_METHOD *sink_method;

static void require(int ok, const char *what) {
    if (!ok) {
        fprintf(stderr, "synthetic: %s\n", what);
        ERR_print_errors_fp(stderr);
        abort();
    }
}
static void no_network(void) {
    require(0, "unexpected socket/connection/read operation");
}
int __wrap_socket(int domain, int type, int protocol) { no_network(); return -1; }
int __wrap_socketpair(int domain, int type, int protocol, int sv[2]) { no_network(); return -1; }
int __wrap_connect(int fd, const struct sockaddr *addr, socklen_t len) { no_network(); return -1; }
ssize_t __wrap_recv(int fd, void *buf, size_t len, int flags) { no_network(); return -1; }
ssize_t __wrap_recvfrom(int fd, void *buf, size_t len, int flags, struct sockaddr *a, socklen_t *n) { no_network(); return -1; }
ssize_t __wrap_recvmsg(int fd, struct msghdr *msg, int flags) { no_network(); return -1; }

static void consume(const void *data, size_t len, int encrypted) {
    Fixture *f = active;
    require(f != NULL, "write without fixture");
    if (f->capture) {
        require(len <= CAPTURE_SIZE - f->captured_len, "capture overflow");
        memcpy(f->captured + f->captured_len, data, len);
        f->captured_len += len;
    }
#ifdef SYNTH_DIAGNOSTICS
    if (encrypted) {
        const unsigned char *p = data;
        size_t offset = 0;
        cipher_bytes += len;
        while (offset < len) {
            require(len - offset >= 5, "incomplete TLS record header");
            size_t n = 5 + ((size_t)p[offset+3] << 8) + p[offset+4];
            require(n <= len-offset, "incomplete TLS record");
            records++;
            offset += n;
        }
    }
#endif
}
ssize_t __wrap_send(int fd, const void *data, size_t len, int flags) {
    require(fd == FAKE_FD, "unexpected send descriptor");
#ifdef SYNTH_DIAGNOSTICS
    writes++;
#endif
    consume(data, len, 0);
    return len;
}
ssize_t __wrap_sendmsg(int fd, const struct msghdr *msg, int flags) {
    size_t length = 0;
    require(fd == FAKE_FD, "unexpected sendmsg descriptor");
#ifdef SYNTH_DIAGNOSTICS
    writes++;
#endif
    for (size_t i=0; i<msg->msg_iovlen; i++) {
        consume(msg->msg_iov[i].iov_base,msg->msg_iov[i].iov_len,0);
        length += msg->msg_iov[i].iov_len;
    }
    return length;
}
#ifdef SYNTH_DIAGNOSTICS
int __real_SSL_write(SSL *, const void *, int);
int __wrap_SSL_write(SSL *ssl, const void *data, int len) {
    tls_writes++;
    return __real_SSL_write(ssl,data,len);
}
#endif
static int sink_write(BIO *bio, const char *data, int len) {
    consume(data,(size_t)len,1);
    return len;
}
static int sink_create(BIO *bio) { BIO_set_init(bio,1); return 1; }
static int sink_destroy(BIO *bio) { return 1; }
static long sink_ctrl(BIO *bio, int cmd, long num, void *ptr) {
    return cmd == BIO_CTRL_FLUSH ? 1 : 0;
}
static void initialize_tls(void) {
    if (client_ctx) return;
    client_ctx = SSL_CTX_new(TLS_client_method());
    server_ctx = SSL_CTX_new(TLS_server_method());
    require(client_ctx && server_ctx,"SSL contexts");
    SSL_CTX *contexts[] = {client_ctx,server_ctx};
    for (size_t i=0;i<2;i++) {
        require(SSL_CTX_set_min_proto_version(contexts[i],TLS1_3_VERSION),"TLS min");
        require(SSL_CTX_set_max_proto_version(contexts[i],TLS1_3_VERSION),"TLS max");
        require(SSL_CTX_set_ciphersuites(contexts[i],"TLS_AES_128_GCM_SHA256"),"cipher suite");
    }
    SSL_CTX_set_num_tickets(server_ctx,0);
    EVP_PKEY_CTX *key_ctx=EVP_PKEY_CTX_new_id(EVP_PKEY_EC,NULL);
    EVP_PKEY *key=NULL;
    require(key_ctx && EVP_PKEY_keygen_init(key_ctx)>0 &&
            EVP_PKEY_CTX_set_ec_paramgen_curve_nid(key_ctx,NID_X9_62_prime256v1)>0 &&
            EVP_PKEY_keygen(key_ctx,&key)>0,"test key");
    X509 *cert=X509_new();
    require(cert != NULL,"test certificate");
    X509_set_version(cert,2);
    ASN1_INTEGER_set(X509_get_serialNumber(cert),1);
    X509_gmtime_adj(X509_get_notBefore(cert),0);
    X509_gmtime_adj(X509_get_notAfter(cert),86400);
    X509_set_pubkey(cert,key);
    X509_NAME *name=X509_get_subject_name(cert);
    X509_NAME_add_entry_by_txt(name,"CN",MBSTRING_ASC,(unsigned char *)"synthetic",-1,-1,0);
    X509_set_issuer_name(cert,name);
    require(X509_sign(cert,key,EVP_sha256())>0 && SSL_CTX_use_certificate(server_ctx,cert) &&
            SSL_CTX_use_PrivateKey(server_ctx,key),"test credentials");
    X509_free(cert); EVP_PKEY_free(key); EVP_PKEY_CTX_free(key_ctx);
    sink_method=BIO_meth_new(BIO_TYPE_SOURCE_SINK|BIO_get_new_index(),"synthetic discard");
    require(sink_method && BIO_meth_set_write(sink_method,sink_write) &&
            BIO_meth_set_ctrl(sink_method,sink_ctrl) && BIO_meth_set_create(sink_method,sink_create) &&
            BIO_meth_set_destroy(sink_method,sink_destroy),"discard BIO");
}
static void transfer(BIO *from, BIO *to) {
    char buf[16384];
    while (BIO_ctrl_pending(from)>0) {
        int n=BIO_read(from,buf,sizeof(buf));
        require(n>0 && BIO_write(to,buf,n)==n,"memory handshake transfer");
    }
}
static void handshake_step(SSL *ssl) {
    if (SSL_is_init_finished(ssl)) return;
    int ret=SSL_do_handshake(ssl);
    int error=SSL_get_error(ssl,ret);
    require(ret==1 || error==SSL_ERROR_WANT_READ || error==SSL_ERROR_WANT_WRITE,"memory handshake");
}
Fixture *synth_new(int tls) {
    Fixture *f=calloc(1,sizeof(*f));
    require(f != NULL,"fixture allocation");
    f->conn=pqMakeEmptyPGconn();
    require(f->conn != NULL,"empty connection");
    f->conn->status=CONNECTION_OK;
    f->conn->sock=FAKE_FD;
    f->conn->nonblocking=true;
    f->conn->sigpipe_flag=true;
    if (tls) {
        initialize_tls();
        SSL *client=SSL_new(client_ctx), *server=SSL_new(server_ctx);
        require(client && server,"SSL sessions");
        SSL_set_bio(client,BIO_new(BIO_s_mem()),BIO_new(BIO_s_mem()));
        SSL_set_bio(server,BIO_new(BIO_s_mem()),BIO_new(BIO_s_mem()));
        SSL_set_connect_state(client); SSL_set_accept_state(server);
        for (int i=0;i<1000 && (!SSL_is_init_finished(client)||!SSL_is_init_finished(server));i++) {
            handshake_step(client); transfer(SSL_get_wbio(client),SSL_get_rbio(server));
            handshake_step(server); transfer(SSL_get_wbio(server),SSL_get_rbio(client));
        }
        require(SSL_is_init_finished(client)&&SSL_is_init_finished(server),"handshake completion");
        transfer(SSL_get_wbio(client),SSL_get_rbio(server));
        transfer(SSL_get_wbio(server),SSL_get_rbio(client));
        require(strcmp(SSL_get_cipher_name(client),"TLS_AES_128_GCM_SHA256")==0,"negotiated cipher");
        SSL_set0_wbio(client,BIO_new(sink_method));
        f->conn->ssl=client; f->conn->ssl_in_use=true;
        f->peer=server;
    }
    active=f;
    return f;
}
PGconn *synth_connection(Fixture *f) { active=f; return f->conn; }
void synth_capture(Fixture *f) { active=f; f->capture=1; f->captured_len=f->decoded_len=0; }
const unsigned char *synth_output(Fixture *f, size_t *len) {
    if (!f->peer) { *len=f->captured_len; return f->captured; }
    require(BIO_write(SSL_get_rbio(f->peer),f->captured,f->captured_len)==(int)f->captured_len,"ciphertext delivery");
    while (BIO_ctrl_pending(SSL_get_rbio(f->peer)) || SSL_pending(f->peer)) {
        size_t n=0;
        require(SSL_read_ex(f->peer,f->decoded+f->decoded_len,CAPTURE_SIZE-f->decoded_len,&n)==1,"TLS decryption");
        require(n>0,"decrypted bytes");
        f->decoded_len+=n;
    }
    *len=f->decoded_len;
    return f->decoded;
}
void synth_complete(Fixture *f) {
    require(PQflush(f->conn)==0,"complete flush");
    require(!f->conn->borrowHead && !f->conn->borrowBytes && !f->conn->outCount,"empty output queues");
    pqCommandQueueAdvance(f->conn,true,false);
    require(!f->conn->cmd_queue_head,"empty command queue");
    f->conn->asyncStatus=PGASYNC_IDLE;
}
int synth_send(Fixture *f, int borrowed, const char *body, const PQbuffer *parts, size_t count) {
    active=f;
    if (borrowed) {
        PQborrowedParam param={parts,count,0,0};
        return PQsendQueryPreparedBorrowed(f->conn,"bench",1,&param,0);
    }
    const char *values[]={body};
    return PQsendQueryPrepared(f->conn,"bench",1,values,NULL,NULL,0);
}
void synth_free(Fixture *f) {
    f->conn->sock=PGINVALID_SOCKET;
    f->conn->status=CONNECTION_BAD;
    PQfinish(f->conn);
    if (f->peer) SSL_free(f->peer);
    free(f);
    active=NULL;
}
void synth_cleanup(void) {
    SSL_CTX_free(client_ctx); SSL_CTX_free(server_ctx); BIO_meth_free(sink_method);
    client_ctx=server_ctx=NULL; sink_method=NULL;
}
double synth_cpu(void) {
    struct timespec ts; require(clock_gettime(CLOCK_PROCESS_CPUTIME_ID,&ts)==0,"CPU clock");
    return ts.tv_sec + ts.tv_nsec/1e9;
}
void synth_metrics_reset(void) { writes=tls_writes=records=cipher_bytes=0; }
uint64_t synth_metric(int index) {
    switch(index) { case 0:return writes; case 1:return tls_writes; case 2:return records; default:return cipher_bytes; }
}
const char *synth_openssl(void) { return OpenSSL_version(OPENSSL_VERSION); }
