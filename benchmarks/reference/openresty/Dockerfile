ARG OPENRESTY_IMAGE=openresty/openresty:1.27.1.2-0-bookworm-fat
FROM ${OPENRESTY_IMAGE} AS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends luarocks libssl-dev git build-essential && rm -rf /var/lib/apt/lists/*
RUN luarocks --lua-version=5.1 install lpeg 1.1.0-2 && luarocks --lua-version=5.1 install pgmoon 1.18.0-1 && luarocks --lua-version=5.1 install lua-resty-openssl 1.8.0-1
FROM ${OPENRESTY_IMAGE}
COPY --from=dependencies /usr/local/share/lua/5.1 /usr/local/share/lua/5.1
COPY --from=dependencies /usr/local/lib/lua/5.1 /usr/local/lib/lua/5.1
WORKDIR /app
COPY app.lua ./
EXPOSE 8080 8443
CMD ["openresty", "-c", "/bench/openresty.conf", "-g", "daemon off;"]
