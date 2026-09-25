FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "-m", "lab_operator"]
