FROM pgxio-libpq:borrowed
WORKDIR /synthetic
COPY benchmarks/synthetic ./
COPY crates/pgxio-module/src/input.rs /crates/pgxio-module/src/input.rs
COPY crates/pgxio-module/src/borrowed.rs /crates/pgxio-module/src/borrowed.rs
COPY crates/pgxio-module/src/buffer.rs /crates/pgxio-module/src/buffer.rs
# Path includes are relative to /synthetic/src; mirror the repository layout.
RUN mkdir -p /crates/pgxio-module/src
RUN --mount=type=cache,target=/usr/local/cargo/registry cargo build --release --locked && cp target/release/pgxio-borrowed-synthetic /usr/local/bin/borrowed-synthetic && cargo build --release --locked --features diagnostics && cp target/release/pgxio-borrowed-synthetic /usr/local/bin/borrowed-synthetic-diagnostics
RUN cc -O2 -shared -fPIC allocations.c -o /usr/local/lib/synthetic-allocations.so
RUN (rustc --version; cc --version | head -n 1; sha256sum /opt/pgxio-libpq/lib/libpq.a /postgres/src/interfaces/libpq/fe-exec.c /postgres/src/interfaces/libpq/fe-misc.c /synthetic/fixture.c /crates/pgxio-module/src/input.rs /crates/pgxio-module/src/borrowed.rs /crates/pgxio-module/src/buffer.rs /synthetic/src/main.rs /synthetic/Cargo.lock) > /synthetic/build-meta.txt
ENTRYPOINT ["borrowed-synthetic"]
