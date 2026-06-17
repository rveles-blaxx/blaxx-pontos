"""Configuracoes do app."""
from __future__ import annotations
import os
import re
import sys


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


# Fallback padrão quando DATABASE_URL não está configurada (dev/local).
_DEFAULT_DB_URL = "sqlite:///blaxx.db"


def _clean_pasted_db_url(raw: str) -> str:
    """Remove sujeira comum de colagem no painel (Render/Neon/etc).

    Casos que vimos derrubar o boot ("Could not parse SQLAlchemy URL"):
      * espaço/quebra-de-linha nas pontas → strip;
      * aspas/crase/<> envolvendo o valor inteiro;
      * prefixo de comando colado por engano: `psql 'postgres://...'`;
      * prefixo `DATABASE_URL=` colado junto do valor;
      * QUEBRA DE LINHA OU ESPAÇO NO MEIO da string (colagem multi-linha no
        textarea do painel) — uma URL nunca contém whitespace, então qualquer
        whitespace interno é lixo e é removido.
    """
    s = raw.strip().strip("'\"`")
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    if s.lower().startswith("psql "):
        s = s[5:].strip().strip("'\"`")
    if s.lower().startswith("database_url="):
        s = s.split("=", 1)[1].strip().strip("'\"`")
    # URLs não têm espaços/\n/\t: remove qualquer whitespace interno residual.
    s = re.sub(r"\s+", "", s)
    return s


def _resolve_db_url() -> str:
    """Lê DATABASE_URL tolerando erros comuns de colagem no painel (Render/etc).

    Causa real de boot-crash em prod ("Could not parse SQLAlchemy URL"): a
    variável existe mas vem com espaço/quebra-de-linha/aspas — aí o default de
    os.environ.get() NÃO entra (a chave não está "ausente") e o make_url()
    recebe lixo. Limpamos antes de usar (ver _clean_pasted_db_url):
      * string vazia após limpeza ⇒ cai no SQLite default (boot não quebra);
      * valor não-vazio é validado com make_url() AQUI, com mensagem clara e
        acionável — melhor que o traceback opaco do SQLAlchemy lá no boot.
    """
    raw = os.environ.get("DATABASE_URL") or ""
    cleaned = _clean_pasted_db_url(raw)
    if not cleaned:
        return _DEFAULT_DB_URL
    url = _normalize_db_url(cleaned)
    try:
        from sqlalchemy.engine.url import make_url
        make_url(url)
    except Exception as exc:  # noqa: BLE001 — diagnóstico de boot
        # Diagnóstico SEM vazar o valor (pode conter senha): só metadados.
        had_ws = bool(re.search(r"\s", raw.strip()))
        scheme = url.split("://", 1)[0] if "://" in url else "(sem ://)"
        print(
            "[config] DATABASE_URL inválida após limpeza — boot vai abortar. "
            f"len_bruto={len(raw)} len_limpo={len(cleaned)} "
            f"tinha_whitespace_interno={had_ws} scheme={scheme!r}. "
            "Verifique o valor no painel: deve ser "
            "postgresql://USUARIO:SENHA@HOST/BANCO?sslmode=require "
            "(sem aspas, sem espaços, em uma única linha).",
            file=sys.stderr,
            flush=True,
        )
        raise
    return url


class Config:
    SQLALCHEMY_DATABASE_URI = _resolve_db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Confiabilidade com Neon/serverless: o provedor FECHA conexões ociosas e o
    # pooler derruba SSL, gerando "SSL connection has been closed unexpectedly"
    # (500 no próximo SELECT). pool_pre_ping testa a conexão antes de usar e
    # reconecta; pool_recycle descarta conexões velhas antes do timeout do Neon.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }
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

    # ---------------- Step-up 2FA em operações sensíveis (B13) ----------------
    # Acima deste valor, transferência/resgate exigem o código TOTP — MAS só
    # para usuários que têm 2FA ativo (não-disruptivo p/ quem não configurou).
    SENSITIVE_OP_THRESHOLD_PTS = int(os.environ.get("BLAXX_SENSITIVE_OP_THRESHOLD_PTS", 20_000))

    # ---------------- Alertas de transações suspeitas (B14) ----------------
    ALERT_HIGH_VALUE_PTS = int(os.environ.get("BLAXX_ALERT_HIGH_VALUE_PTS", 30_000))
    ALERT_VELOCITY_COUNT = int(os.environ.get("BLAXX_ALERT_VELOCITY_COUNT", 5))
    ALERT_VELOCITY_WINDOW_MIN = int(os.environ.get("BLAXX_ALERT_VELOCITY_WINDOW_MIN", 10))
    ALERT_DISTINCT_RECIPIENTS = int(os.environ.get("BLAXX_ALERT_DISTINCT_RECIPIENTS", 4))

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

    # ---------------- Níveis de cliente (loyalty tiers) ----------------
    # Nível por PONTOS ACUMULADOS (lifetime) = soma de todos os créditos
    # confirmados no ledger (nunca cai por gastar/resgatar). 4 categorias
    # progressivas. Faixas em pontos (min inclusivo). Sobrescrivível por env.
    #   Bronze 0+ · Prata 5.000+ · Ouro 20.000+ · Black 50.000+
    TIER_BRONZE_MIN = int(os.environ.get("BLAXX_TIER_BRONZE_MIN", 0))
    TIER_PRATA_MIN = int(os.environ.get("BLAXX_TIER_PRATA_MIN", 5_000))
    TIER_OURO_MIN = int(os.environ.get("BLAXX_TIER_OURO_MIN", 20_000))
    TIER_BLACK_MIN = int(os.environ.get("BLAXX_TIER_BLACK_MIN", 50_000))

    @classmethod
    def tiers(cls) -> list[dict]:
        """Definição canônica dos 4 níveis (ordem crescente)."""
        return [
            {"key": "bronze", "label": "Bronze", "min_points": cls.TIER_BRONZE_MIN,
             "color": "#CD7F32", "text_color": "#FFFFFF",
             "perks": "Acesso ao programa, catálogo de benefícios e PIX."},
            {"key": "prata", "label": "Prata", "min_points": cls.TIER_PRATA_MIN,
             "color": "#9AA0A6", "text_color": "#0B0B0C",
             "perks": "Ofertas exclusivas Prata + atendimento prioritário."},
            {"key": "ouro", "label": "Ouro", "min_points": cls.TIER_OURO_MIN,
             "color": "#D4AF37", "text_color": "#0B0B0C",
             "perks": "Bônus em campanhas + benefícios premium."},
            {"key": "black", "label": "Black", "min_points": cls.TIER_BLACK_MIN,
             "color": "#0B0B0C", "text_color": "#C6F432",
             "perks": "Tudo do Ouro + experiências Black e limites VIP."},
        ]

    # BlaXx VIP — categoria FORA da escala por pontos (não é atingida
    # acumulando pontos). É concedida apenas por convite (admin seta is_vip).
    # Benefícios: compras de pontos SEM limite (vide services/purchase.py),
    # exchange preferencial e concierge. min_points é um sentinela alto só
    # para manter o tipo Int nos clientes (SwiftUI/JSON); a UI mostra
    # "Por convite" para a chave 'vip'.
    VIP_TIER = {
        "key": "vip", "label": "BlaXx VIP", "min_points": 999_999_999,
        "color": "#0A0A0A", "text_color": "#C6F432", "invite_only": True,
        "perks": "Compras de pontos ilimitadas, exchange preferencial e "
                 "concierge dedicado — exclusivo, apenas por convite.",
    }

    @classmethod
    def tiers_catalog(cls) -> list[dict]:
        """Catálogo COMPLETO de categorias para exibição: os 4 níveis por
        pontos + BlaXx VIP (por convite) no topo."""
        return cls.tiers() + [cls.VIP_TIER]

    @classmethod
    def tier_for_points(cls, lifetime_points: int) -> dict:
        """Retorna o nível atual para um total de pontos acumulados."""
        current = cls.tiers()[0]
        for t in cls.tiers():
            if lifetime_points >= t["min_points"]:
                current = t
        return current

    @classmethod
    def tier_progress(cls, lifetime_points: int) -> dict:
        """Nível atual + próximo + quanto falta (pontos e %)."""
        tiers = cls.tiers()
        current = cls.tier_for_points(lifetime_points)
        idx = next(i for i, t in enumerate(tiers) if t["key"] == current["key"])
        nxt = tiers[idx + 1] if idx + 1 < len(tiers) else None
        if nxt is None:
            return {
                "lifetime_points": lifetime_points,
                "current": current, "next": None,
                "points_to_next": 0, "progress_pct": 100,
            }
        span = nxt["min_points"] - current["min_points"]
        gained = lifetime_points - current["min_points"]
        pct = 100 if span <= 0 else max(0, min(100, int(gained * 100 / span)))
        return {
            "lifetime_points": lifetime_points,
            "current": current, "next": nxt,
            "points_to_next": max(0, nxt["min_points"] - lifetime_points),
            "progress_pct": pct,
        }

    # ---------------- Apple Wallet (PassKit) ----------------
    # Geração do cartão Blaxx como .pkpass para a carteira do iPhone.
    # O .pkpass precisa ser ASSINADO com um certificado Pass Type ID emitido
    # pela Apple (conta Apple Developer). Enquanto os certificados não forem
    # configurados, o backend monta o pass mas NÃO assina — o endpoint
    # /card/pass responde 503 com instrução clara (frontends mostram "em breve").
    #
    # Para ativar, configure no Render (Environment) e suba os arquivos:
    #   APPLE_PASS_TYPE_ID        = pass.com.blaxx.cartao   (Identifier do Pass Type ID)
    #   APPLE_TEAM_ID             = ABCDE12345              (Apple Developer Team ID)
    #   APPLE_PASS_CERT_PATH      = /etc/secrets/pass.p12   (cert + chave privada, formato PKCS#12)
    #   APPLE_PASS_CERT_PASSWORD  = ********                (senha do .p12)
    #   APPLE_WWDR_CERT_PATH      = /etc/secrets/wwdr.pem   (Apple WWDR intermediate G4, PEM)
    #   APPLE_PASS_ORG_NAME       = Blaxx Pontos
    APPLE_PASS_TYPE_ID = os.environ.get("APPLE_PASS_TYPE_ID", "")
    APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", "")
    APPLE_PASS_CERT_PATH = os.environ.get("APPLE_PASS_CERT_PATH", "")
    APPLE_PASS_CERT_PASSWORD = os.environ.get("APPLE_PASS_CERT_PASSWORD", "")
    APPLE_WWDR_CERT_PATH = os.environ.get("APPLE_WWDR_CERT_PATH", "")
    APPLE_PASS_ORG_NAME = os.environ.get("APPLE_PASS_ORG_NAME", "Blaxx Pontos")

    @classmethod
    def apple_pass_configured(cls) -> bool:
        """True quando todos os segredos p/ assinar o .pkpass estão presentes."""
        return all([
            cls.APPLE_PASS_TYPE_ID, cls.APPLE_TEAM_ID,
            cls.APPLE_PASS_CERT_PATH, cls.APPLE_WWDR_CERT_PATH,
        ])

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
