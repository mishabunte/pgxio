ARG GO_IMAGE=golang:1.25.1-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-slim
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags='-s -w' -o /server .
FROM ${RUNTIME_IMAGE}
RUN useradd --uid 10001 --no-create-home bench
COPY --from=build /server /server
USER bench
EXPOSE 8080 8443
ENTRYPOINT ["/server"]
