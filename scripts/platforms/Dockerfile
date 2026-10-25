ARG BASE_IMAGE=debian:13-slim
FROM ${BASE_IMAGE} AS toolchain
COPY scripts/platforms/bootstrap.sh /bootstrap.sh
RUN sh /bootstrap.sh
COPY benchmarks/requirements.txt /requirements.txt
COPY sdk/python /python-sdk
RUN python3 -m venv /opt/test-python && /opt/test-python/bin/pip install -r /requirements.txt '/python-sdk[django]'
FROM toolchain AS build
WORKDIR /source
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY examples ./examples
COPY patches ./patches
COPY scripts ./scripts
COPY tests ./tests
COPY sql ./sql
COPY deploy ./deploy
COPY sdk/python ./sdk/python
COPY benchmarks/requirements.txt /source/benchmarks/requirements.txt
ARG LIBPQ_VARIANT=system
ARG CARGO_FEATURES=
RUN python3 scripts/platforms/build.py --prefix /opt/pgxio --libpq "$LIBPQ_VARIANT" --features "$CARGO_FEATURES"
RUN python3 -m venv /opt/test-python && /opt/test-python/bin/pip install -r benchmarks/requirements.txt './sdk/python[django]'
# Test image retains the compiler/source for standalone C contracts and fault shims.
FROM build AS test
ARG LIBPQ_VARIANT=system
ARG CARGO_FEATURES=
LABEL io.pgxio.libpq-variant="$LIBPQ_VARIANT" io.pgxio.cargo-features="$CARGO_FEATURES"
ENTRYPOINT ["/opt/test-python/bin/python", "/source/tests/run-native.py"]

FROM ${BASE_IMAGE} AS runtime
ARG LIBPQ_VARIANT=system
COPY scripts/platforms/runtime-deps.sh /runtime-deps.sh
RUN sh /runtime-deps.sh
COPY --from=build /opt/pgxio/bin /usr/local/bin
COPY --from=build /opt/pgxio/modules /usr/local/nginx/modules
COPY --from=build /opt/pgxio/libpq /usr/local/nginx/libpq
COPY deploy/nginx.conf /etc/nginx/nginx.conf
LABEL io.pgxio.config-format="nginx-v1" io.pgxio.libpq-variant="$LIBPQ_VARIANT"
USER 10001
EXPOSE 8080
STOPSIGNAL SIGQUIT
ENTRYPOINT ["nginx"]
CMD ["-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"]
