"""Extensões compartilhadas (instanciadas sem app, ligadas no factory)."""

import os

from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter

db = SQLAlchemy()
jwt = JWTManager()


def _trusted_proxy_hops() -> int:
    """Quantos proxies confiaveis ficam na frente do app.

    Render/Heroku = 1. Se houver Cloudflare na frente do Render, 2.
    Configuravel por TRUSTED_PROXY_HOPS; minimo 1 (nunca confiar no valor
    que o cliente escreveu no inicio da cadeia).
    """
    try:
        return max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
    except (TypeError, ValueError):
        return 1


def _real_client_ip() -> str:
    """Key function do rate limiter que respeita X-Forwarded-For atras de proxy.

    No Render/Fly toda request passa pelo edge proxy, entao
    `request.remote_addr` vira o IP interno do proxy (algo como 10.x ou
    172.16.x), igual pra todos os clientes. Isso faz o rate limit virar
    cap global — funciona pra alguns casos (webhook PSP) mas e' inutil
    pra abuso por cliente (login, charge, transfer).

    Ordem de prioridade dos headers:
      1. Fly-Client-IP  — Fly garante ser o peer real (mais confiavel)
      2. X-Forwarded-For — Render/Heroku/Cloudflare; ver nota de seguranca
      3. request.remote_addr — fallback final

    SEGURANCA: o PRIMEIRO elemento de X-Forwarded-For e' escrito pelo
    cliente; o proxy da plataforma anexa o IP real no FIM da cadeia. Ler o
    primeiro elemento deixava o atacante escolher a propria chave de rate
    limit (spraying de senha com XFF rotativo). Contamos TRUSTED_PROXY_HOPS
    a partir do fim: com 1 hop (default do Render), o valor correto e' o
    ultimo elemento.

    Sprint 1 (2026-05-28): portado de blaxx_app — o canonico ainda usava
    `get_remote_address` que ignora proxies, deixando o rate limit
    efetivamente global.
    """
    fly_ip = request.headers.get("Fly-Client-IP", "").strip()
    if fly_ip:
        return fly_ip
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            hops = _trusted_proxy_hops()
            # -hops a partir do fim; se a cadeia for menor que o esperado,
            # cai no primeiro elemento disponivel (nunca estoura o indice).
            idx = max(0, len(parts) - hops)
            return parts[idx]
    return request.remote_addr or "unknown"


limiter = Limiter(
    key_func=_real_client_ip,
    default_limits=[],          # default global desligado; controle vem dos decorators
    storage_uri="memory://",    # sobrescrito no factory via app.config
)
