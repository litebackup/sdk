# syntax=docker/dockerfile:1.10
FROM debian:bookworm-slim
ARG TARGETARCH
ARG LITEBACKUP_VERSION
ARG LITEBACKUP_SHA256
ADD --chmod=0755 --checksum=sha256:${LITEBACKUP_SHA256} \
    https://litebackup-cdn.com/litebackup/${LITEBACKUP_VERSION}/litebackup-linux-${TARGETARCH} \
    /usr/local/bin/litebackup
RUN litebackup version
