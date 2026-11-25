# The image scripts/test runs the Ruby SDK's tests in: a pinned Ruby image
# with the compiler and headers that the native extensions of the development
# gems (psych, nio4r, sqlite3 from source) need.
ARG RUBY_IMAGE
FROM ${RUBY_IMAGE}
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libyaml-dev git \
    && rm --recursive --force /var/lib/apt/lists/*
