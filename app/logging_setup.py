"""Sprint 5 (S5-4) · Setup de logging estruturado JSON + Datadog APM.

Em prod, troca o output texto-plano por JSON estruturado, que:
  - vai pro Render Dashboard como linha JSON parseavel
  - aceita ingest direto em Datadog/CloudWatch/Loki sem regex pra
    extrair campos
  - mantem campos consistentes: timestamp, level, logger, message,
    request_id, user_id (quando disponivel)

Em dev, mantem texto plano legivel (humanos).

Datadog APM (ddtrace) e' lazy-init: so ativa se DD_API_KEY estiver
setado. Nao adiciona overhead em dev.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any


def _is_dev() -> bool:
    return (os.environ.get("FLASK_ENV") == "development"
            or os.environ.get("FLASK_DEBUG") == "1"
            or os.environ.get("TESTING"))


def init_logging(level: str | None = None) -> None:
    """Configura root logger com formato JSON em prod, texto em dev.

    Chamado uma vez no startup do app. Idempotente — se ja configurado,
    nao duplica handlers.
    """
    if getattr(init_logging, "_done", False):
        return

    lvl_str = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_str, logging.INFO)

    root = logging.getLogger()
    # Remove handlers existentes (gunicorn pode ter setado um)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if _is_dev():
        # Texto humano legivel
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        # JSON estruturado
        try:
            from pythonjsonlogger import jsonlogger
            fmt = jsonlogger.JsonFormatter(
                # Campos default sempre presentes
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level",
                               "name": "logger"},
                json_ensure_ascii=False,
            )
        except ImportError:
            # Sem python-json-logger instalado, cai pra texto + warning
            fmt = logging.Formatter(
                '{"timestamp":"%(asctime)s","level":"%(levelname)s",'
                '"logger":"%(name)s","message":"%(message)s"}',
                datefmt="%Y-%m-%dT%H:%M:%S",
            )

    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(lvl)

    # Reduz ruido de libs barulhentas
    for noisy in ("werkzeug", "urllib3", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    init_logging._done = True
    root.info("logging inicializado · level=%s · format=%s",
              lvl_str, "text" if _is_dev() else "json")


def init_datadog_apm() -> None:
    """Inicializa Datadog APM (ddtrace) se DD_API_KEY estiver setado.

    Patch automatico de Flask, SQLAlchemy, requests. Nao requer mudanca
    no codigo. Spans aparecem no Datadog Dashboard apos rodar com:

        DD_API_KEY=xxx DD_SERVICE=blaxx-pontos DD_ENV=prod \\
        ddtrace-run gunicorn -w 2 wsgi:app

    Alternativa: chamamos `patch_all()` manualmente, mas o ddtrace-run e'
    o caminho oficial pra incluir pre-spans (BootstrapFinished).
    """
    if not os.environ.get("DD_API_KEY"):
        return
    try:
        from ddtrace import patch_all
        patch_all(flask=True, sqlalchemy=True, requests=True)
        logging.getLogger("blaxx.ddtrace").info("Datadog APM ativado via patch_all")
    except ImportError:
        logging.getLogger("blaxx.ddtrace").info(
            "DD_API_KEY setada mas ddtrace nao instalado. "
            "Pip install ddtrace + ddtrace-run pra ativar."
        )


def add_request_context_filter() -> None:
    """Adiciona um filter que injeta request_id e user_id em todo log.

    Usar dentro do request context. Chamado pelo create_app no
    @before_request. Pra logs fora do request fica sem esses campos.
    """
    from flask import g, has_request_context, request

    class RequestContextFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if has_request_context():
                record.request_id = (request.headers.get("X-Request-ID")
                                     or request.headers.get("X-Correlation-ID")
                                     or "")
                u = getattr(g, "current_user", None)
                record.user_id = getattr(u, "id", "") if u else ""
                record.path = request.path
                record.method = request.method
            else:
                record.request_id = ""
                record.user_id = ""
                record.path = ""
                record.method = ""
            return True

    for h in logging.getLogger().handlers:
        h.addFilter(RequestContextFilter())
