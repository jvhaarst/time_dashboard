# Status page for a stratum-1 NTP server. Multi-arch: the cluster that runs it is
# arm64 Raspberry Pis plus one amd64 node.
#
# Two stages so uv and the lockfile never reach the published image. Both bases are
# pinned by digest, which is the only form Renovate can track for a tag that gets
# rebuilt in place -- `python:3.15-slim` moves under you, the digest does not.

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.15-slim@sha256:08a4c22c92b79ec821a7ad49768480039ee5b7accac3955228e75366e30abc8e AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, from the lockfile alone, so a change to the application code does
# not invalidate this layer. `--frozen` fails rather than silently re-resolving: a
# lockfile that does not match pyproject.toml should break the build, not the runtime.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app


FROM python:3.15-slim@sha256:08a4c22c92b79ec821a7ad49768480039ee5b7accac3955228e75366e30abc8e

# Non-root, and no shell for the account: the process needs to read nothing but its own
# code and to open one TCP port.
RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 10001 app

WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --from=build --chown=root:root /app/app /app/app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
EXPOSE 8080

# Two sync workers, not one: a single worker blocks the whole site for the duration of
# one slow VictoriaMetrics query. Gunicorn's own timeout is above the app's query
# timeout (VM_TIMEOUT, 5 s) so a slow query returns an error page rather than having its
# worker killed mid-request.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app.main:app"]
