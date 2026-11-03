# Runtime-only image for the community gateway.
#
# The binary is produced ahead of time by build.sh (or shipped in the release
# bundle). Nothing in this file needs Go, Node or bun, so `docker build` works
# on any machine that only has Docker installed.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata wget \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Run as an unprivileged user. /data is a named volume, so Docker seeds its
# ownership from this directory and the process can always write to it.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/logs \
    && chown -R 10001:10001 /data

COPY --chmod=0755 bin/new-api /usr/local/bin/new-api
COPY --chmod=0644 licenses/ /licenses/

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:3000/api/status || exit 1

ENTRYPOINT ["/usr/local/bin/new-api"]
CMD ["--port", "3000", "--log-dir", "/data/logs"]
