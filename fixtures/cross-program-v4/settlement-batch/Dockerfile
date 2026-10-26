FROM node:22-alpine
WORKDIR /app
COPY --chown=node:node package.json ./package.json
COPY --chown=node:node src ./src
COPY --chown=node:node readers ./readers
COPY --chown=node:node pipeline ./pipeline
COPY --chown=node:node fixtures ./fixtures
COPY --chown=node:node config/settlement-rules.json ./config/settlement-rules.json
USER node
CMD ["node", "src/job.js"]
