FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY services/control-plane/src ./services/control-plane/src
COPY skills/packs/general-research.yaml ./skills/packs/general-research.yaml
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/services/control-plane/src" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "scilab.api.runtime:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
