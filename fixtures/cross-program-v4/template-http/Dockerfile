FROM node:22-alpine
WORKDIR /app
COPY --chown=node:node package.json ./package.json
COPY --chown=node:node server.js ./server.js
COPY --chown=node:node view ./view
COPY --chown=node:node routes ./routes
COPY --chown=node:node views/index.html ./views/index.html
USER node
CMD ["node", "server.js"]
