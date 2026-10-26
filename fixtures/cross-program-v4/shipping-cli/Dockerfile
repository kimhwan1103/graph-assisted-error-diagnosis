FROM node:22-alpine
WORKDIR /app
COPY --chown=node:node package.json ./package.json
COPY --chown=node:node bin ./bin
COPY --chown=node:node lib ./lib
COPY --chown=node:node pricing ./pricing
COPY --chown=node:node assets/tariffs.json ./assets/tariffs.json
USER node
CMD ["node", "bin/quote.js"]
