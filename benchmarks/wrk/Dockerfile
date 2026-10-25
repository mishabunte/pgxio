FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates libssl-dev libluajit-5.1-dev luajit && rm -rf /var/lib/apt/lists/*
COPY monotonic-clock.pl /monotonic-clock.pl
ARG WRK_REF=4.2.0
ARG WRK2_REF=44a94c17d8e6a0bac8559b53da76848e430cb7a7
RUN git clone https://github.com/wg/wrk.git /src/wrk && cd /src/wrk && git checkout "$WRK_REF" && perl /monotonic-clock.pl src/wrk.c src/ae.c && make -j2 WITH_OPENSSL=/usr WITH_LUAJIT=/usr CFLAGS='-std=c99 -O2 -I/usr/include/luajit-2.1 -D_POSIX_C_SOURCE=200809L -D_DEFAULT_SOURCE -D_REENTRANT' && cp wrk /usr/local/bin/wrk && git rev-parse HEAD > /wrk-revision
# Use the system LuaJIT on both architectures; wrk2's old bundled LuaJIT does
# not support modern ARM64 builders. Match the original Makefile's two paths.
RUN ln -s /usr/bin/luajit /usr/include/luajit-2.1/luajit && ln -s /usr/lib/$(gcc -dumpmachine)/libluajit-5.1.a /usr/include/luajit-2.1/libluajit.a
# ARM64 defaults to unsigned char; getopt's EOF must remain a signed int.
# These two old HdrHistogram includes are unused; dropping them preserves its
# algorithm while allowing ARM64/system-LuaJIT builds.
RUN git clone https://github.com/giltene/wrk2.git /src/wrk2 && cd /src/wrk2 && git checkout "$WRK2_REF" && perl /monotonic-clock.pl src/wrk.c src/ae.c && sed -i '/#include <x86intrin.h>/d; /#include <zlib.h>/d' src/hdr_histogram.c && sed -i 's/char c, \*\*header = headers;/int c; char **header = headers;/' src/wrk.c && make -j2 LDIR=/usr/include/luajit-2.1 LIBS='-lluajit-5.1 -lpthread -lm -lcrypto -lssl -ldl' CFLAGS='-std=c99 -Wall -O2 -D_REENTRANT -D_DEFAULT_SOURCE -D_POSIX_C_SOURCE=200809L -DluaL_reg=luaL_Reg -I/usr/include/luajit-2.1 -include sys/time.h' && cp wrk /usr/local/bin/wrk2 && git rev-parse HEAD > /wrk2-revision
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libluajit-5.1-2 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/wrk /usr/local/bin/wrk2 /usr/local/bin/
COPY --from=build /wrk-revision /wrk2-revision /usr/local/share/
LABEL io.pgxbench.clock="CLOCK_MONOTONIC"
CMD ["wrk2", "--help"]
