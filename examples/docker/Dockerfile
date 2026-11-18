FROM node:24-bookworm-slim AS build
WORKDIR /src
COPY typescript/package*.json ./typescript/
RUN cd typescript && npm ci --omit=optional
COPY typescript ./typescript
RUN cd typescript && npm run build

FROM node:24-bookworm-slim
WORKDIR /src/examples/docker
COPY --from=build /src/typescript/dist/src /src/typescript/dist/src
COPY typescript/package.json /src/typescript/package.json
COPY examples/docker/package*.json ./
RUN npm ci --omit=dev --omit=optional
COPY --from=cli /usr/local/bin/litebackup /usr/local/bin/litebackup
ENV LITEBACKUP_PATH=/usr/local/bin/litebackup
RUN litebackup start --help >/dev/null
COPY examples/docker/app.mjs ./
RUN mkdir -p /data /state && chown node:node /data /state
ENV XDG_STATE_HOME=/state XDG_RUNTIME_DIR=/tmp/litebackup-runtime
USER node
EXPOSE 3000
CMD ["node", "app.mjs"]
