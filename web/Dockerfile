FROM rust:1-alpine AS build

RUN apk add --no-cache musl-dev cmake make clang perl
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src/ src/
RUN cargo build --release --locked

FROM alpine:3

RUN apk add --no-cache ca-certificates
ENV CONFIG_PATH=/config/config.yaml

WORKDIR /app
COPY --from=build /src/target/release/bus-stop-web .
COPY static/ static/

USER nobody
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget -q -T 4 -O /dev/null http://127.0.0.1:8000/healthz
CMD ["./bus-stop-web"]
