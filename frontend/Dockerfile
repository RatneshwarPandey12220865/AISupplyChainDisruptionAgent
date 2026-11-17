FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
# API_URL is read at *runtime* by the server layout and handed to the browser, so the same image works on any
# domain: docker run -e API_URL=https://api.example.com ...
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0 API_URL=http://localhost:8000
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
CMD ["node", "server.js"]
