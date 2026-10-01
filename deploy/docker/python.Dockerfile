# syntax=docker/dockerfile:1.7
# One image for every Python deployable; the command selects the service (Helm sets it).
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY libs ./libs
COPY services ./services
COPY scripts ./scripts
COPY ml ./ml
COPY chaos ./chaos

FROM python:3.12-slim-bookworm AS runtime
# libgomp is LightGBM's OpenMP runtime; nothing else is added to the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin tally
WORKDIR /app
COPY --from=build --chown=root:root /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "services.core.api:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "25"]
