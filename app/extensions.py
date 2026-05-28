"""Extensões compartilhadas (instanciadas sem app, ligadas no factory)."""

from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter

db = SQLAlchemy()
jwt = JWTManager()


def _real_client_ip() -> str:
    """Key function do rate limiter que respeita X-Forwarded-For atras de proxy.

    No Render/Fly toda request passa pelo edge proxy, entao
    `request.remote_addr` vira o IP interno do proxy (algo como 10.x ou
    172.16.x), igual pra todos os clientes. Isso faz o rate limit virar
    cap global — funciona pra alguns casos (webhook PSP) mas e' inutil
    pra abuso por cliente (login, charge, transfer).

    Ordem de prioridade dos headers:
      1. Fly-Client-IP  — Fly garante ser o peer real (mais confiavel)
      2. X-Forwarded-For — Render/Heroku/Cloudflare; pegamos o primeiro
      3. request.remote_addr — fallback final

    Sprint 1 (2026-05-28): portado de blaxx_app — o canonico ainda usava
    `get_remote_address` que ignora proxies, deixando o rate limit
    efetivamente global.
    """
    fly_ip = request.headers.get("Fly-Client-IP", "").strip()
    if fly_ip:
        return fly_ip
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


limiter = Limiter(
    key_func=_real_client_ip,
    default_limits=[],          # default global desligado; controle vem dos decorators
    storage_uri="memory://",    # sobrescrito no factory via app.config
)
