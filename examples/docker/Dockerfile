FROM node:24-bookworm-slim
WORKDIR /app
COPY examples/docker/package.json examples/docker/package-lock.json ./
RUN npm ci --omit=dev
COPY examples/docker/app.mjs ./
RUN mkdir -p /data /state && chown node:node /data /state
ENV XDG_STATE_HOME=/state XDG_RUNTIME_DIR=/tmp/litebackup-runtime
USER node
EXPOSE 3000
CMD ["node", "app.mjs"]
