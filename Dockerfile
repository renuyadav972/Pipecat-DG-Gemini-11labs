FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# Create static directory if not present
RUN mkdir -p static

EXPOSE 7860

CMD ["uv", "run", "uvicorn", "outbound.server:app", "--host", "0.0.0.0", "--port", "7860"]
