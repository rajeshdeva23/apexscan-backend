# ---------------------------------------------------------------------------
# ApexScan frontend — development image (Vite dev server).
# A separate multi-stage production image (static build served by nginx) can
# be added when deploying.
# ---------------------------------------------------------------------------
FROM node:22-alpine

WORKDIR /app

# Install dependencies first for layer caching.  Use the committed lockfile so
# Docker builds resolve the same dependency graph as the frontend CI gate.
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the source.
COPY . .

EXPOSE 5173

# Bind to all interfaces so the port is reachable from the host/proxy.
CMD ["npm", "run", "dev", "--", "--host"]
