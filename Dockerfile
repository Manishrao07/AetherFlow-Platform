# syntax=docker/dockerfile:1
# Multi-purpose image: `runtime` is what ships; `test` adds dev tools for `make docker-test`.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# WHY copy pyproject + app first and `pip install .`: the `app` package is installed into
# site-packages, so `import app...` works from ANY working directory (scripts, alembic, tests),
# and dependency layers are cached until pyproject changes.
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

# WHY non-root: a compromised process should not own the container.
# WHY chmod: COPY keeps the host's file modes. A checkout with private modes (0600, common after
# unpacking an archive or on some macOS setups) would leave files unreadable to the non-root user,
# e.g. Alembic failing with "Permission denied: 'pyproject.toml'". Read-only for everyone is enough.
RUN useradd --system --uid 10001 --no-create-home appuser \
    && chmod -R a+rX /srv
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# WHY --factory: settings are validated when the app is built, and fail fast at boot.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# ---- UI image -------------------------------------------------------------------------------
# WHY a separate stage that does NOT build on `runtime`: the UI needs Streamlit (large) and none of
# the API's dependencies, and it must never be able to import the database layer. It only speaks
# HTTP to the API, so the two images stay small and independently deployable.
FROM python:3.12-slim AS ui

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_BASE_URL=http://api:8000

WORKDIR /srv
COPY ui/requirements.txt ./ui-requirements.txt
RUN pip install -r ui-requirements.txt
COPY ui ./ui
# Streamlit reads ./.streamlit/config.toml relative to the working directory.
COPY ui/.streamlit ./.streamlit

# WHY non-root with a home: Streamlit writes small files under $HOME.
# WHY chmod: same reason as the runtime stage (host file modes are preserved by COPY).
RUN useradd --system --uid 10002 --create-home uiuser \
    && chmod -R a+rX /srv
USER uiuser

EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2)"
CMD ["streamlit", "run", "ui/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]

FROM runtime AS test
USER root
COPY tests ./tests
COPY ui ./ui
RUN pip install ".[dev]" && chmod -R a+rX /srv
USER appuser
CMD ["pytest"]
