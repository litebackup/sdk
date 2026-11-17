FROM node:24-bookworm-slim
WORKDIR /opt/litebackup-sdk
COPY typescript/package.json typescript/package-lock.json ./typescript/
RUN cd typescript && npm ci
COPY typescript ./typescript
RUN cd typescript && npm run build
RUN mkdir -p /state && chown node:node /state
ENV XDG_STATE_HOME=/state XDG_RUNTIME_DIR=/tmp/litebackup-runtime
USER node
ENTRYPOINT ["node", "typescript/dist/examples/one-shot.js"]
