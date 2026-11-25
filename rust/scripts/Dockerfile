# The Rust SDK's test image: a pinned rust image with Clippy and rustfmt, and
# on the stable toolchain cargo-deny. scripts/test builds it per toolchain.
ARG RUST_IMAGE=rust:1.100.0-bookworm
FROM ${RUST_IMAGE}
ARG CARGO_DENY_VERSION=
RUN rustup component add clippy rustfmt \
    && if [ -n "${CARGO_DENY_VERSION}" ]; then \
        cargo install --locked --root /usr/local cargo-deny --version "${CARGO_DENY_VERSION}"; \
    fi
