FROM ghcr.io/astral-sh/uv:0.11.11 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY deploy/check_provider.py /app/check_provider.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HOME=/home/provider
RUN useradd --create-home --home-dir /home/provider provider
USER provider
ENTRYPOINT ["provider-server"]
