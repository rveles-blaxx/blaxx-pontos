"""Blaxx Pontos — backend Flask.

Módulo que entrega 3 funcionalidades centrais:
  1. Compra de pontos via PIX (cobrança PIX → webhook → crédito)
  2. Envio de pontos entre clientes inscritos (P2P interno)
  3. Resgate de pontos via PIX (payout PIX → débito)

A integração PIX é feita via interface abstrata (`app.pix.provider.PixProvider`)
com implementação mock (`app.pix.mock.MockPixProvider`) — pronta para ser
substituída por um provedor real (Mercado Pago, Asaas, Efí, Stark Bank, etc.)
sem mudar nenhuma regra de negócio.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, send_from_directory
from flask_cors import CORS

from .config import Config
from .extensions import db, jwt, limiter
from .pix.mock import MockPixProvider
from .pix.mercadopago import MercadoPagoPixProvider

# Pasta renderer/ do app Electron (relativo a app/__init__.py)
SITE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "renderer"))


def create_app(config: type[Config] | None = None, pix_provider=None) -> Flask:
    # static_folder serve o QR PIX (e qualquer outro asset) em /static/*
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.from_object(config or Config)

    db.init_app(app)
    jwt.init_app(app)

    # Onda 1 P0: callback que checa se JWT foi revogado (logout)
    @jwt.token_in_blocklist_loader
    def _check_revoked(jwt_header, jwt_payload):
        from .models import RevokedToken
        jti = jwt_payload.get("jti")
        if not jti:
            return False
        return db.session.get(RevokedToken, jti) is not None

    # Rate limiter — apenas se habilitado (Config.RATELIMIT_ENABLED)
    if app.config.get("RATELIMIT_ENABLED", True):
        limiter.storage_uri = app.config["RATELIMIT_STORAGE_URI"]
        limiter.init_app(app)

    # CORS - libera o front (Netlify) chamar a API (Fly.io).
    # Garantia: sempre inclui o domínio Netlify mesmo se CORS_ORIGINS env var
    # estiver definido com lista parcial. Resolve "Failed to fetch" no Google
    # Login que vinha do preflight OPTIONS sendo bloqueado.
    configured_origins = app.config.get("CORS_ORIGINS") or []
    if configured_origins == ["*"]:
        origins_setting = "*"
    else:
        required_origins = {
            "https://blaxxpontos.netlify.app",
            "https://blaxxpontos.com",       # caso configure domínio próprio
            "https://www.blaxxpontos.com",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        }
        merged = sorted(set(configured_origins) | required_origins)
        origins_setting = merged
    CORS(
        app,
        resources={r"/*": {"origins": origins_setting}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Request-ID"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        max_age=600,
    )

    # ─── Headers de segurança (Onda 2 — Spec do user, seção 6) ──────────
    # Usa middleware manual (after_request). É mais portátil e nunca quebra
    # o startup — Flask-Talisman tem assinatura de kwargs que muda entre
    # versões (TypeError em x_frame_options/x_content_type_options se versão
    # antiga). O fallback abaixo cobre os essenciais sem dependência externa.
    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=(), payment=()")
        # CSP estrito — backend é só JSON, nada de inline scripts/styles
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'none'; frame-ancestors 'none'")
        return resp

    # PIX provider — Sprint 3: seleção via env var PIX_PROVIDER
    if pix_provider is None:
        provider_name = app.config.get("PIX_PROVIDER", "mock").lower()
        if provider_name == "mercadopago":
            mp_token = app.config.get("MP_ACCESS_TOKEN", "")
            if not mp_token:
                app.logger.error(
                    "PIX_PROVIDER=mercadopago mas MP_ACCESS_TOKEN está vazio. "
                    "Caindo no MockPixProvider."
                )
                pix_provider = MockPixProvider()
            else:
                pix_provider = MercadoPagoPixProvider(
                    access_token=mp_token,
                    notification_url=app.config.get("MP_NOTIFICATION_URL") or None,
                )
                app.logger.info("PIX provider: MercadoPago")
        else:
            pix_provider = MockPixProvider()
            app.logger.info("PIX provider: Mock (demo)")
    app.extensions["pix_provider"] = pix_provider

    # Blueprints
    from .api.auth import bp as auth_bp
    from .api.wallet import bp as wallet_bp
    from .api.pix import bp as pix_bp
    from .api.transfer import bp as transfer_bp
    from .api.redeem import bp as redeem_bp
    from .api.partners import bp as partners_bp
    from .api.benefits import bp_benefits, bp_vouchers
    from .api.campaigns import bp as campaigns_bp
    from .api.notifications import bp as notifications_bp
    from .api.admin import bp as admin_bp
    from .api.security import bp as security_bp, register_login_2fa_route

    # Onda 3 — registra /auth/login/2fa NO blueprint auth_bp existente
    # (mantém prefix /auth/* coerente, sem precisar criar segundo blueprint).
    register_login_2fa_route(auth_bp)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(wallet_bp, url_prefix="/wallet")
    app.register_blueprint(pix_bp, url_prefix="/pix")
    app.register_blueprint(transfer_bp, url_prefix="/transfer")
    app.register_blueprint(redeem_bp, url_prefix="/redeem")
    app.register_blueprint(partners_bp, url_prefix="/partners")
    app.register_blueprint(bp_benefits, url_prefix="/benefits")
    app.register_blueprint(bp_vouchers, url_prefix="/vouchers")
    app.register_blueprint(campaigns_bp, url_prefix="/campaigns")
    app.register_blueprint(notifications_bp, url_prefix="/notifications")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    # Onda 3 — telefone + 2FA SMS + sessões + access-log
    app.register_blueprint(security_bp, url_prefix="/user")

    with app.app_context():
        db.create_all()
        _apply_lightweight_migrations(app)

    # ----- Healthchecks (Wave 6 — robustez operacional) ---------------------
    # /health    → resposta rica: uptime, versao, timestamp. Usado pelo Render
    #              health check, monitores externos, banner de offline do frontend.
    # /healthz   → alias minimalista (convencao Kubernetes/cloud). Mesmo handler.
    # /livez     → liveness probe — o processo esta vivo? (sempre 200 se servir)
    # /readyz    → readiness probe — pronto pra receber trafego? checa DB rapido.
    #
    # Todos sao publicos (sem auth), sem rate limit, e nao tocam recursos caros.
    # Custo: <2ms cada. Safe pra monitor externo pingar a cada 1min.
    _process_start_ts = datetime.now(timezone.utc)

    @app.get("/health")
    @app.get("/healthz")
    def health():
        uptime_s = int((datetime.now(timezone.utc) - _process_start_ts).total_seconds())
        return {
            "status": "ok",
            "service": "blaxx-pontos-backend",
            "uptime_s": uptime_s,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/livez")
    def livez():
        # Liveness — so confirma que o processo Python esta respondendo.
        # NUNCA falha — se o handler executou, o processo esta vivo.
        return {"alive": True}

    @app.get("/readyz")
    def readyz():
        # Readiness — checa DB com query trivial. Se DB caiu, devolve 503
        # pra monitor parar de mandar trafego (em setup com load balancer).
        try:
            from sqlalchemy import text
            db.session.execute(text("SELECT 1"))
            return {"ready": True}
        except Exception as e:
            app.logger.warning("readyz: DB indisponivel: %s", e)
            return {"ready": False, "reason": "db_unavailable"}, 503

    # ----- Servir o frontend (renderer/) na mesma origem (modo web, sem Electron) -----
    @app.get("/app/")
    def app_root():
        return send_from_directory(SITE_DIR, "index.html")

    @app.get("/app/<path:filename>")
    def app_file(filename: str):
        return send_from_directory(SITE_DIR, filename)

    # Aliases curtos para compatibilidade
    @app.get("/site/")
    def site_root():
        return send_from_directory(SITE_DIR, "index.html")

    @app.get("/site/<path:filename>")
    def site_file(filename: str):
        return send_from_directory(SITE_DIR, filename)

    # ----- Servir o prototipo /blaxx/ (Netlify) no mesmo backend -----
    BLAXX_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "blaxx"))

    @app.get("/blaxx/")
    def blaxx_root():
        return send_from_directory(BLAXX_DIR, "index.html")

    @app.get("/blaxx/<path:filename>")
    def blaxx_file(filename: str):
        return send_from_directory(BLAXX_DIR, filename)

    @app.get("/")
    def index():
        """Pagina simples listando os endpoints (so para debug no navegador)."""
        from flask import jsonify
        return jsonify({
            "service": "blaxx-pontos-backend",
            "version": "0.1.0",
            "pix_provider": app.extensions["pix_provider"].name,
            "endpoints": {
                "GET  /health":                  "healthcheck",
                "POST /auth/register":           "criar conta (name, email, cpf, password)",
                "POST /auth/login":              "login (email|cpf + password) -> Bearer token",
                "GET  /auth/me":                 "perfil do usuario logado",
                "GET  /wallet/":                 "saldo da carteira",
                "GET  /wallet/transactions":     "extrato (parametros: ?limit=N)",
                "GET  /pix/packages":            "pacotes de pontos disponiveis",
                "POST /pix/charge":              "criar cobranca PIX para comprar pontos",
                "POST /pix/simulate-payment":    "[mock] forcar pagamento de uma charge",
                "POST /transfer/":               "enviar pontos a outro cliente (P2P)",
                "GET  /redeem/quote":            "cotar resgate (parametro: ?points=N)",
                "POST /redeem/":                 "solicitar resgate via PIX",
                "GET  /partners/":               "lista de parceiros (?category, ?q)",
                "GET  /partners/categories":     "categorias disponiveis",
                "GET  /partners/<id>":           "detalhe do parceiro + beneficios",
                "GET  /benefits/":               "catalogo de beneficios",
                "GET  /benefits/<id>":           "detalhe do beneficio",
                "POST /benefits/<id>/redeem":    "resgatar beneficio (gera voucher)",
                "GET  /vouchers/":               "vouchers do usuario logado",
                "GET  /vouchers/<id>":           "detalhe do voucher",
                "GET  /campaigns/":              "campanhas ativas",
                "POST /campaigns/<id>/join":     "aderir a uma campanha",
                "POST /campaigns/<id>/progress": "[demo] avancar progresso (amount_brl)",
                "GET  /campaigns/mine":          "campanhas que aderi",
                "GET  /notifications/":          "minhas notificacoes (in-app)",
                "GET  /notifications/unread-count": "contador de nao lidas",
                "PATCH /notifications/<id>/read":  "marcar como lida",
                "POST /notifications/read-all":  "marcar todas como lidas",
            },
            "demo_users": [
                {"email": "mariana@blaxx.com", "password": "123456", "balance_pts": 84750},
                {"email": "lucas@blaxx.com",   "password": "123456", "balance_pts":  5000},
            ],
            "tip": "Para testar todos os fluxos de uma vez, rode 'testar-fluxos.bat'",
        })

    return app


def _apply_lightweight_migrations(app):
    """Adiciona colunas faltantes via ALTER TABLE quando o schema antigo já existir.

    Cobre Onda 3 (SMS MFA): users.phone_verified, users.mfa_method.
    Idempotente — checa existência antes. Funciona em SQLite e PostgreSQL.
    Para mudanças complexas (drop column, type change), trocar por Alembic.
    """
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    if "users" not in insp.get_table_names():
        return  # nada a migrar — db.create_all() já criou tudo

    existing_cols = {c["name"] for c in insp.get_columns("users")}
    pending = [
        # (column, ddl) — defaults compatíveis com SQLite e PG
        ("phone_verified", "BOOLEAN NOT NULL DEFAULT 0"),
        ("mfa_method", "VARCHAR(16)"),
    ]
    driver = db.engine.url.drivername
    is_pg = driver.startswith("postgres") or driver.startswith("psycopg")
    with db.engine.begin() as conn:
        for col, ddl in pending:
            if col in existing_cols:
                continue
            # PG aceita FALSE; SQLite só aceita 0
            pg_ddl = ddl.replace("DEFAULT 0", "DEFAULT FALSE") if is_pg else ddl
            try:
                conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {col} {pg_ddl}")
                app.logger.info("migration: added users.%s", col)
            except Exception as e:
                app.logger.warning("migration: failed adding users.%s (%s)", col, e)
