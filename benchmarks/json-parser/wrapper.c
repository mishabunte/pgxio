#define JSMN_STRICT
#define JSMN_PARENT_LINKS
#include "jsmn.h"
int bench_jsmn(const unsigned char *data, size_t len, jsmntok_t *tokens, unsigned int cap) {
    jsmn_parser parser;
    jsmn_init(&parser);
    return jsmn_parse(&parser, (const char *)data, len, tokens, cap);
}
size_t bench_token_size(void) { return sizeof(jsmntok_t); }
