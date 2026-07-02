# LawBey MCP server — production image.
# Uses uv for dependency management. Secrets come from Fly secrets at runtime.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install uv via the official slim image method.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (better layer caching).
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project

# Copy the source.
COPY src ./src
COPY README.md ./

# Install the project itself.
RUN uv sync --no-dev

# Fly sets PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Run the FastAPI app via uvicorn.
CMD ["sh", "-c", "uv run uvicorn lawbey_mcp.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
