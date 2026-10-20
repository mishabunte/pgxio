# The module and Nginx must have matching headers and configure options.
FROM rust:slim-trixie AS build
ARG NGINX_VERSION=1.30.5
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates git libclang-dev libpcre2-dev libssl-dev zlib1g-dev libpq-dev pkg-config && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar xz -C /tmp && mv /tmp/nginx-${NGINX_VERSION} /nginx
WORKDIR /nginx
RUN ./configure --prefix=/usr/local/nginx --with-compat --with-http_ssl_module --with-http_v2_module --with-http_stub_status_module --error-log-path=stderr --http-log-path=/dev/stdout --pid-path=/tmp/nginx.pid --http-client-body-temp-path=/tmp/client_body --http-proxy-temp-path=/tmp/proxy && make -j2
ENV NGINX_SOURCE_DIR=/nginx NGINX_BUILD_DIR=/nginx/objs
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY examples ./examples
ARG CARGO_FEATURES=""
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/usr/local/cargo/git --mount=type=cache,target=/build/target cargo test --release --locked --test core --test allocations --features "$CARGO_FEATURES" -- --nocapture && cargo build --release --locked --features "$CARGO_FEATURES" && cp target/release/libngx_http_pgxio_module.so /module.so

FROM debian:trixie-slim
LABEL io.pgxio.config-format="nginx-v1" io.pgxio.output-validation="off"
LABEL io.pgxio.rest-input-validation="postgres"
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 libssl3t64 libpcre2-8-0 zlib1g ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home pgxio && mkdir -p /tmp/client_body /tmp/proxy /usr/local/nginx/logs && chown -R pgxio /tmp/client_body /tmp/proxy /usr/local/nginx
COPY --from=build /nginx/objs/nginx /usr/local/bin/nginx
COPY --from=build /module.so /usr/local/nginx/modules/libngx_http_pgxio_module.so
COPY deploy/nginx.conf /etc/nginx/nginx.conf
USER 10001
EXPOSE 8080
STOPSIGNAL SIGQUIT
ENTRYPOINT ["nginx"]
CMD ["-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"]
