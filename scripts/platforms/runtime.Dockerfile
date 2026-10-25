ARG BASE_IMAGE=debian:13-slim
FROM ${BASE_IMAGE}
COPY runtime-deps.sh /runtime-deps.sh
RUN sh /runtime-deps.sh
COPY install/bin /usr/local/bin
COPY install/modules /usr/local/nginx/modules
COPY install/libpq /usr/local/nginx/libpq
COPY nginx.conf /etc/nginx/nginx.conf
ARG CARGO_FEATURES=
LABEL io.pgxio.config-format="nginx-v1" io.pgxio.cargo-features="$CARGO_FEATURES"
USER 10001
EXPOSE 8080
STOPSIGNAL SIGQUIT
ENTRYPOINT ["nginx"]
CMD ["-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"]
