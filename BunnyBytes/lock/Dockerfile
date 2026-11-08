# bunny-lock: Rocsy's shared GPU queue (Rust). See PROTOCOL.md.
FROM rust:1-slim-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
# Dependencies first, so editing src/ doesn't rebuild them.
RUN mkdir src && echo 'fn main() {}' > src/main.rs && cargo build --release --locked && rm -rf src
COPY src ./src
RUN touch src/main.rs && cargo build --release --locked

# Distroless: glibc + libgcc only (rustls and bundled SQLite need nothing else), no shell.
# uid 1000 = the host user owning the bind-mounted data/ folder.
FROM gcr.io/distroless/cc-debian12
WORKDIR /app
COPY --from=build /src/target/release/bunny-lock /usr/local/bin/bunny-lock
COPY --chown=1000:1000 templates ./templates
COPY --chown=1000:1000 static ./static
USER 1000:1000
ENV LOCK_PORT=8600 LOCK_DATA_DIR=/app/data LOCK_STATIC_DIR=/app/static LOCK_TEMPLATE=/app/templates/queue.html

# Binds 127.0.0.1 only (network_mode: host). Two trust surfaces:
#  - internal /v2/... (loopback + X-Queue-Internal-Secret), used by the apps'
#    backends through BunnyBytes/shared/bunnylock.py;
#  - browser /Queue/... reached only through bunny-gateway, which checks the
#    session cookie before proxying.
EXPOSE 8600
CMD ["bunny-lock"]
