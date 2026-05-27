# =============================================================================
# Blaxx Pontos · backend · imagem Docker para Fly.io
# =============================================================================
# CACHE BUSTER 2026-05-25-google-login (bumpa este número sempre que alterar
# requirements.txt pra GARANTIR que pip install rebuilda do zero).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências primeiro (cache de camada melhor).
# psycopg[binary]>=3.2 é self-contained: o wheel ja inclui libpq compilada,
# então NÃO precisamos instalar libpq5/build-essential via apt — o que evita
# depender de DNS pra deb.debian.org (que falha em alguns ambientes WSL2).
# https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation
COPY requirements.txt .
# Verifica imediatamente que google-auth foi instalado, falha o build se não.
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -c "from google.oauth2 import id_token; from google.auth.transport import requests as gr; import requests; print('OK Google OAuth stack')" \
    && python -c "import psycopg; print('OK psycopg', psycopg.__version__)"

# Código da aplicação
COPY . .

# Fly.io seta $PORT (default 8080 no fly.toml abaixo)
ENV PORT=8080
EXPOSE 8080

# Healthcheck do container (Fly também faz HTTP /health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen(f'http://127.0.0.1:{__import__(\"os\").environ.get(\"PORT\",\"8080\")}/health', timeout=3); sys.exit(0)" \
    || exit 1

# Entry: cria tabelas e sobe o gunicorn.
# Seed roda separado via `fly ssh console -C "python seed.py"`.
CMD sh -c "python -c 'from app import create_app; create_app()' && \
           gunicorn --bind 0.0.0.0:${PORT} \
                    --workers 2 \
                    --threads 4 \
                    --timeout 60 \
                    --access-logfile - \
                    --error-logfile - \
                    run:app"
