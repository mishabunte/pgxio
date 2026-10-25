/* C fixture/libpq sanitizer validation; never used for performance timing. */
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <libpq-fe.h>
typedef struct Fixture Fixture;
Fixture *synth_new(int);
void synth_capture(Fixture *);
const unsigned char *synth_output(Fixture *,size_t *);
void synth_complete(Fixture *);
int synth_send(Fixture *,int,const char *,const PQbuffer *,size_t);
void synth_free(Fixture *);
void synth_cleanup(void);
int main(void) {
    const size_t sizes[]={256,1024,4096,16384,65536}, counts[]={1,4,16};
    unsigned cases=0;
    for(int tls=0;tls<2;tls++) for(size_t s=0;s<5;s++) for(size_t c=0;c<3;c++) {
        size_t size=sizes[s],count=counts[c];
        char *body=malloc(size+1); memset(body,'x',size); body[size]=0;
        PQbuffer *parts=calloc(count,sizeof(*parts));
        for(size_t i=0;i<count;i++) {
            parts[i].len=(i+1)*size/count-i*size/count;
            char *chunk=malloc(parts[i].len);
            memcpy(chunk,body+i*size/count,parts[i].len); parts[i].data=chunk;
        }
        for(int borrowed=0;borrowed<2;borrowed++) {
            Fixture *f=synth_new(tls); synth_capture(f);
            assert(synth_send(f,borrowed,body,parts,count)==1); synth_complete(f);
            size_t len; const unsigned char *output=synth_output(f,&len);
            assert(len>size && output[0]=='B');
            /* Prepared name, format-count field, format code when explicit,
             * argument count and argument length precede the payload. */
            size_t start=5+1+6+2+(borrowed?2:0)+2+4;
            assert(len>=start+size && memcmp(output+start,body,size)==0);
            synth_free(f); cases++;
        }
        for(size_t i=0;i<count;i++)free((void *)parts[i].data);
        free(parts); free(body);
    }
    synth_cleanup();
    printf("{\"validated_cases\":%u,\"socket_operations\":0}\n",cases);
    return 0;
}
