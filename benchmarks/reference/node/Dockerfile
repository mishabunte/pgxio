ARG NODE_IMAGE=node:22.20.0-bookworm-slim
FROM ${NODE_IMAGE}
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY server.mjs ./
USER node
EXPOSE 8080 8443
CMD ["node", "server.mjs"]
