"""Configuracoes do app."""
from __future__ import annotations
import os


def _normalize_db_url(url: str) -> str:
    """Aceita formatos do Heroku/Neon/Supabase e ajusta para psycopg v3.

    - postgres://...  → postgresql+psycopg://...
    - postgresql://...→ postgresql+psycopg://...
    - sqlite://... fica intacto
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get("DATABASE_URL", "sqlite:///blaxx.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")

    # ---------------- JWT ----------------
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
    # Sprint 1: access token reduzido de 24h para 30min (padrao fintech).
    # Refresh token continua 30 dias. O frontend usa /auth/refresh para
    # renovar silenciosamente. Sobrescrivivel via env BLAXX_JWT_ACCESS_MIN.
    JWT_ACCESS_TOKEN_EXPIRES = int(os.environ.get("BLAXX_JWT_ACCESS_MIN", 30)) * 60
    JWT_REFRESH_TOKEN_EXPIRES = 60 * 60 * 24 * 30 # 30 dias
    JWT_TOKEN_LOCATION = ["headers"]

    # ---------------- SMS (Twilio) — Onda 3 ----------------
    # Em dev, deixe SMS_BACKEND=console e o código sai no log do server.
    SMS_BACKEND = os.environ.get("SMS_BACKEND", "console")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_PHONE = os.environ.get("TWILIO_FROM_PHONE", "")
    PHONE_OTP_TTL = int(os.environ.get("PHONE_OTP_TTL", "600"))         # 10 min verify
    MFA_CHALLENGE_TTL = int(os.environ.get("MFA_CHALLENGE_TTL", "300")) # 5 min login
    PHONE_OTP_COOLDOWN = int(os.environ.get("PHONE_OTP_COOLDOWN", "60"))

    # ---------------- Rate limiter ----------------
    # storage_uri: em produção idealmente Redis; em dev/sandbox usa memória
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "200 per minute")
    # Desliga rate limit em testes (TestConfig sobrescreve)
    RATELIMIT_ENABLED = True

    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
    ] or ["*"]

    # URL base do front-end (web SPA). Usada para montar links absolutos em
    # e-mails (ex.: reset de senha -> {FRONTEND_URL}/redefinir-senha?token=...).
    # Em prod, setar no Render. Default = deploy Netlify do SPA React.
    FRONTEND_URL = os.environ.get(
        "FRONTEND_URL", "https://blaxx-pontos-app.netlify.app"
    ).rstrip("/")

    # ---------------- Conversao ponto <-> R$ ----------------
    # 1 ponto = R$ 0,09 = 9 centavos. Mantemos o conversor em CENTAVOS para
    # evitar floats no ledger. Toda matematica usa CENTS_PER_POINT.
    #   pts -> cents: pts * CENTS_PER_POINT
    #   cents -> pts: cents // CENTS_PER_POINT  (floor; resto fica como house edge)
    #   pts -> BRL:   pts * CENTS_PER_POINT / 100  (display)
    CENTS_PER_POINT = int(os.environ.get("BLAXX_CENTS_PER_POINT", 9))

    # ---------------- Limites de resgate ----------------
    # Usuarios VIP nao tem teto diario (vide redeem.py).
    # Demais usuarios: teto em R$ convertido para pontos.
    REDEEM_MIN_POINTS = int(os.environ.get("BLAXX_REDEEM_MIN_POINTS", 1))
    # R$ 100.000,00 / R$ 0,09 = 1.111.111 pts. Arredondado pra cima.
    REDEEM_MAX_POINTS_PER_DAY = int(os.environ.get(
        "BLAXX_REDEEM_MAX_POINTS_PER_DAY", 1_111_111
    ))

    # ---------------- Limites de envio (transfer) ----------------
    TRANSFER_MIN_POINTS = 100
    TRANSFER_MAX_POINTS_PER_DAY = 50_000
    PIX_CHARGE_TTL_SECONDS = 30 * 60

    @classmethod
    def brl_per_point(cls) -> float:
        """Display helper: R$ por ponto (ex: 0.09)."""
        return cls.CENTS_PER_POINT / 100.0

    @classmethod
    def pts_to_cents(cls, pts: int) -> int:
        return pts * cls.CENTS_PER_POINT

    @classmethod
    def cents_to_pts(cls, cents: int) -> int:
        """Floor: 1000 cents / 9 = 111 pts (1 cent fica como house edge)."""
        return cents // cls.CENTS_PER_POINT

    @classmethod
    def rate_label(cls) -> str:
        """String human-readable usada no /redeem/quote."""
        return f"1 pt = R$ {cls.brl_per_point():.2f}".replace(".", ",")

    # ---------------- PIX provider selection ----------------
    # Sprint 3: define qual provider PIX é instanciado pelo app factory.
    # "mock"        → MockPixProvider (default, demo)
    # "mercadopago" → MercadoPagoPixProvider (requer MP_ACCESS_TOKEN)
    PIX_PROVIDER = os.environ.get("PIX_PROVIDER", "mock").lower()
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")
    MP_NOTIFICATION_URL = os.environ.get("MP_NOTIFICATION_URL", "")
    # Anti-replay no webhook MP: janela máxima em segundos entre o `ts` da
    # assinatura e o agora. 300s = 5 min. Aumenta só em janelas de manutenção.
    MP_WEBHOOK_MAX_CLOCK_SKEW = int(os.environ.get("MP_WEBHOOK_MAX_CLOCK_SKEW", 300))

    # ---------------- Google OAuth ----------------
    # Client IDs criados em https://console.cloud.google.com → Credenciais.
    # WEB: usado pelo site Netlify (Google Identity Services no browser).
    # IOS: usado pelo app Mac/iOS via ASWebAuthenticationSession.
    # Backend valida o ID token contra QUALQUER um dos clients confiáveis.
    # Não precisa secret pra validar id_tokens — só pra fluxo authorization-code.
    #
    # Compat: aceita GOOGLE_CLIENT_ID (single) como fallback de
    # GOOGLE_WEB_CLIENT_ID, pra simplificar setup em Render/Heroku/etc onde
    # só queremos um nome de variável.
    #
    # Defaults públicos: estes Client IDs NÃO são segredos — já estão
    # embutidos no binário do app iOS/Mac, no Info.plist e no bundle JS do
    # site. Usá-los como fallback evita que o login Google quebre quando a
    # variável de ambiente não está setada no Render (causa do bug "funciona
    # no site, falha no app": o IOS_CLIENT_ID não estava configurado, então o
    # id_token do app — cujo aud é o client iOS — caía em audience mismatch).
    # A validação de assinatura, expiração, issuer e email_verified continua
    # intacta; o aud segue sendo conferido contra estes IDs específicos.
    GOOGLE_WEB_CLIENT_ID_DEFAULT = (
        "1086156839608-779t8vpo7ht2mb3kajg8qdj3k2mhq75f.apps.googleusercontent.com"
    )
    GOOGLE_IOS_CLIENT_ID_DEFAULT = (
        "105341431878-3msc2p3tjk3p5ro6i34d0b0qks3nf9dj.apps.googleusercontent.com"
    )
    GOOGLE_WEB_CLIENT_ID = (
        os.environ.get("GOOGLE_WEB_CLIENT_ID")
        or os.environ.get("GOOGLE_CLIENT_ID")
        or GOOGLE_WEB_CLIENT_ID_DEFAULT
    )
    GOOGLE_IOS_CLIENT_ID = (
        os.environ.get("GOOGLE_IOS_CLIENT_ID") or GOOGLE_IOS_CLIENT_ID_DEFAULT
    )

    @classmethod
    def google_allowed_audiences(cls) -> list[str]:
        """Lista de audiences (client_ids) aceitos pelo /auth/google."""
        return [a for a in (cls.GOOGLE_WEB_CLIENT_ID, cls.GOOGLE_IOS_CLIENT_ID) if a]

    # ---------------- PIX webhook security ----------------
    # Segredo compartilhado com o gateway PIX para validar HMAC nos webhooks.
    # Cada provedor (Mercado Pago, Efí, Stark, etc) tem o seu próprio header e
    # algoritmo - aqui mantemos o esquema genérico HMAC-SHA256 do body.
    # Sprint 4 (S4-10) · Versao atual dos documentos legais.
    # Quando atualizar termos/privacidade/LGPD, bumpe TERMS_CURRENT_VERSION.
    # No proximo login, usuarios com user.terms_accepted_version != atual
    # serao redirecionados pra re-aceitar antes de continuar.
    # Convencao: "1.0", "1.1" (patch sem mudanca material), "2.0" (mudanca
    # material que exige re-aceite explicito).
    TERMS_CURRENT_VERSION = os.environ.get("BLAXX_TERMS_VERSION", "1.0")

    PIX_WEBHOOK_SECRET = os.environ.get("PIX_WEBHOOK_SECRET", "")

    # Sprint 3 (S3-9) · Lista de IPs permitidos para webhook PIX.
    # Default = vazio = aceita qualquer origem (DEV apenas).
    # PRODUCAO: setar PIX_WEBHOOK_ALLOWED_IPS com a lista oficial do PSP.
    #
    # Mercado Pago — IPs documentados (consulte
    # https://www.mercadopago.com.br/developers/pt/docs/your-integrations/notifications/webhooks
    # antes de deploy — MP pode trocar):
    #   209.225.49.0/24      CIDR principal de webhooks
    #   216.33.196.0/24      CIDR backup
    #   34.195.33.241        Cluster MP-Argentina
    #   34.195.183.18        idem
    #
    # Outros PSPs:
    #   Asaas:   consultar https://docs.asaas.com/docs/webhooks
    #   Stark:   consultar https://starkbank.com/docs (IPs por banco emissor)
    #   Efi:     consultar https://dev.efipay.com.br/docs/api-pix/webhooks
    PIX_WEBHOOK_ALLOWED_IPS = [
        ip.strip() for ip in os.environ.get("PIX_WEBHOOK_ALLOWED_IPS", "").split(",") if ip.strip()
    ]

    # Pacotes — pts mantidos, precos recalculados ao novo rate (R$ 0,09/pt).
    # Plus/Prime/Black mantem um pequeno desconto progressivo embutido
    # (~5%/10%/15% de bonus implicito sobre o preco face).
    POINT_PACKAGES = {
        "start": {"price_brl": 180.00, "points": 2_000,  "label": "Start"},
        "plus":  {"price_brl": 470.00, "points": 5_500,  "label": "Plus"},   # ~5% bonus
        "prime": {"price_brl": 972.00, "points": 12_000, "label": "Prime"},  # ~10% bonus
        "black": {"price_brl": 2142.00, "points": 28_000, "label": "Black"},  # ~15% bonus
    }


class TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    TESTING = True
    SECRET_KEY = "test"
    JWT_SECRET_KEY = "test-jwt"
    RATELIMIT_ENABLED = False
