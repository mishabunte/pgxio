FROM rust:slim-trixie AS build
WORKDIR /bench
COPY Cargo.toml Cargo.lock ./
COPY build.rs wrapper.c ./
COPY vendor ./vendor
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/bench/target cargo build --release --locked && cp target/release/pgxio-json-parser-bench /parser-bench
RUN rustc -Vv > /compiler.txt && cc --version >> /compiler.txt && cat Cargo.lock > /dependencies.lock
FROM debian:trixie-slim
COPY --from=build /parser-bench /parser-bench
COPY --from=build /compiler.txt /compiler.txt
COPY --from=build /dependencies.lock /dependencies.lock
ENTRYPOINT ["/parser-bench"]
