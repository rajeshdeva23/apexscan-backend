# ---------------------------------------------------------------------------
# ApexScan backend — production-oriented image.
# Slim Python base; dependencies installed first for layer caching.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS base

# Prevent Python from writing .pyc files and buffering stdout/stderr.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System build dependencies (asyncpg / SQLAlchemy compile needs).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application source.
COPY . .

# Immutable build identity: the deploy pipeline passes the exact Git SHA
# (`--build-arg BUILD_SHA=<sha>`); the app reports it at GET /version so the
# running artifact is traceable to a commit. Defaults to "unknown" for local
# builds. No git metadata is required inside the container.
ARG BUILD_SHA=unknown
ENV BUILD_SHA=${BUILD_SHA}

EXPOSE 8000

# Start the ASGI server. Reload is enabled via compose override in dev.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
