FROM node:24-bookworm-slim
WORKDIR /opt/litebackup-sdk
COPY typescript/package.json typescript/package-lock.json ./typescript/
RUN cd typescript && npm ci --omit=optional
COPY typescript ./typescript
RUN cd typescript && npm run build
COPY --from=cli /usr/local/bin/litebackup /usr/local/bin/litebackup
ENV LITEBACKUP_PATH=/usr/local/bin/litebackup
RUN litebackup start --help >/dev/null
RUN mkdir --parents /state && chown node:node /state
ENV XDG_STATE_HOME=/state XDG_RUNTIME_DIR=/tmp/litebackup-runtime
USER node
ENTRYPOINT ["node", "typescript/dist/examples/one-shot.js"]
