"""Validador de env vars no boot — fail-fast com mensagens claras.

Filosofia: se o operador errou uma env (placeholder, formato, valor inválido),
queremos que o backend RECUSE subir já no boot, com mensagem dizendo EXATAMENTE
qual var está errada e por quê. A alternativa (boot OK, crash em runtime no
primeiro request) é o pior dos mundos — foi o que aconteceu no deploy
2026-06-24 (DSN placeholder passou o guard simples e quebrou em runtime).

Como usar (em app/__init__.py):
    from .env_schema import validate_env
    validate_env()  # raise EnvError em produção / só warn em dev

Cada entrada tem: name, required (bool), validator (callable), description.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable


class EnvError(RuntimeError):
    """Erro de validação de env. Mensagem deve ser acionável (diz como corrigir)."""


# ---------------------------------------------------------------------------- #
# Validators reutilizáveis                                                     #
# ---------------------------------------------------------------------------- #
def _looks_like_url(value: str, *, allowed_schemes=("https", "http")) -> bool:
    if not value:
        return False
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://", value)
    return bool(m) and m.group(1).lower() in allowed_schemes


def is_sentry_dsn(value: str) -> tuple[bool, str]:
    """DSN do Sentry tem formato https://KEY@host.ingest.sentry.io/PROJECT_ID."""
    if not _looks_like_url(value, allowed_schemes=("https",)):
        return False, "precisa começar com https://"
    if "@" not in value:
        return False, "precisa conter '@' (separador key/host) — placeholder?"
    if not re.search(r"/\d+/?$", value):
        return False, "precisa terminar com /PROJECT_ID (número)"
    return True, ""


def is_url(value: str) -> tuple[bool, str]:
    if not _looks_like_url(value):
        return False, "precisa ser uma URL http(s)://"
    return True, ""


def is_secret_min_32(value: str) -> tuple[bool, str]:
    if len(value) < 32:
        return False, f"precisa ter ≥32 chars (atual: {len(value)})"
    if value == "dev-only-change-me":
        return False, "ainda é o placeholder de dev — gerar com `openssl rand -hex 32`"
    return True, ""


def is_in(*allowed: str) -> Callable[[str], tuple[bool, str]]:
    def _check(value: str) -> tuple[bool, str]:
        if value not in allowed:
            return False, f"valor deve ser um de: {', '.join(allowed)}"
        return True, ""
    return _check


def is_int_in_range(lo: int, hi: int) -> Callable[[str], tuple[bool, str]]:
    def _check(value: str) -> tuple[bool, str]:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return False, f"precisa ser inteiro entre {lo}..{hi}"
        if not (lo <= n <= hi):
            return False, f"fora do range {lo}..{hi} (recebido: {n})"
        return True, ""
    return _check


def is_stripe_secret(value: str) -> tuple[bool, str]:
    """Chave secreta da Stripe: sk_live_… (produção) ou sk_test_… (teste)."""
    if not value.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
        return False, "deve começar com sk_live_ (produção) ou sk_test_ (teste)"
    if len(value) < 20:
        return False, "parece truncada"
    return True, ""


def is_stripe_webhook_secret(value: str) -> tuple[bool, str]:
    if not value.startswith("whsec_"):
        return False, "deve começar com whsec_ (Stripe → Webhooks → signing secret)"
    return True, ""


def is_stripe_publishable(value: str) -> tuple[bool, str]:
    """Chave PUBLICÁVEL da Stripe (pk_…). Pública por design.

    Validar o prefixo evita o erro clássico de colar a SECRET key aqui — o que
    a exporia no JavaScript do browser.
    """
    if value.startswith(("sk_", "rk_")):
        return False, "isto é uma chave SECRETA — nunca use no frontend. Use pk_…"
    if not value.startswith("pk_"):
        return False, "deve começar com pk_live_ (produção) ou pk_test_ (teste)"
    return True, ""


def is_asaas_key(value: str) -> tuple[bool, str]:
    """API key do Asaas: `$aact_prod_…` (produção) ou `$aact_hmlg_…` (sandbox).

    Validar o prefixo evita o pior erro operacional possível aqui: subir a
    chave de sandbox em produção e "pagar" resgates que nunca saem.
    """
    if not value.startswith("$aact_"):
        return False, "deve começar com $aact_prod_ (produção) ou $aact_hmlg_ (sandbox)"
    if len(value) < 40:
        return False, "parece truncada (chave do Asaas é longa)"
    return True, ""


def is_mp_credential(value: str) -> tuple[bool, str]:
    """Credenciais MP começam com TEST- (sandbox) ou APP_USR- (produção)."""
    if not (value.startswith("TEST-") or value.startswith("APP_USR-")):
        return False, "deve começar com TEST- (sandbox) ou APP_USR- (produção) — placeholder?"
    if len(value) < 20:
        return False, f"curta demais pra credencial MP ({len(value)} chars)"
    return True, ""


# ---------------------------------------------------------------------------- #
# Schema — único lugar pra adicionar nova env                                  #
# ---------------------------------------------------------------------------- #
# Tipo: (name, required_in_prod, validator, descrição-curta-pra-erro)
SCHEMA: list[tuple[str, bool, Callable[[str], tuple[bool, str]], str]] = [
    # Secrets obrigatórios em prod (sem default seguro)
    ("SECRET_KEY",      True,  is_secret_min_32, "chave do Flask (sessions, signing)"),
    ("JWT_SECRET_KEY",  True,  is_secret_min_32, "chave dos JWT access/refresh"),

    # Defaults seguros existem; só validamos formato se OPERADOR setou algo
    ("SENTRY_DSN",      False, is_sentry_dsn,    "DSN do Sentry — observability"),
    ("FRONTEND_URL",    False, is_url,           "base URL do SPA pra emails"),
    ("BLAXX_BACKEND_URL", False, is_url,         "override do backend URL"),

    # Booleans/enums com valores aceitos
    ("MAILER",          False, is_in("console", "resend", "noop"),
     "noop|console|resend"),
    ("PIX_PROVIDER",    False, is_in("mock", "asaas", "mercadopago"),
     "asaas (padrão) | mock (homologação) | mercadopago (LEGADO, em remoção)"),
    ("SMS_BACKEND",     False, is_in("console", "twilio"),
     "console|twilio"),

    # Pagamentos MercadoPago — formato validado se setadas; obrigatoriedade
    # condicional (provider/flag) é checada em validate_env().
    ("MP_ACCESS_TOKEN",  False, is_mp_credential,
     "access token MP (TEST-… ou APP_USR-…)"),
    ("MP_PUBLIC_KEY",    False, is_mp_credential,
     "public key MP (TEST-… ou APP_USR-…) — frontend tokeniza cartão"),
    ("CARD_ENABLED",     False, is_in("0", "1"),
     "0|1 — liga o checkout com cartão de crédito"),
    ("CARD_MAX_INSTALLMENTS", False, is_int_in_range(1, 12),
     "parcelas máximas no cartão (1..12)"),
    ("PAYOUT_MODE",      False, is_in("auto", "manual"),
     "auto|manual — manual = fila admin enquanto não há provider de payout"),

    # Asaas — PIX de saída (resgate). Obrigatoriedade condicional em
    # validate_env(): com PAYOUT_MODE=auto, a key e o token de webhook viram
    # obrigatórios em produção.
    ("ASAAS_API_KEY",    False, is_asaas_key,
     "API key do Asaas ($aact_prod_… em produção, $aact_hmlg_… em sandbox)"),
    ("ASAAS_ENV",        False, is_in("sandbox", "production"),
     "sandbox|production — 'sandbox' NÃO envia PIX de verdade"),
    ("ASAAS_WEBHOOK_TOKEN", False, is_secret_min_32,
     "token do header asaas-access-token (32+ chars) — autentica o webhook"),

    # Stripe — cartão internacional.
    ("STRIPE_API_KEY",   False, is_stripe_secret,
     "chave secreta da Stripe (sk_live_… em produção)"),
    ("STRIPE_WEBHOOK_SECRET", False, is_stripe_webhook_secret,
     "signing secret do webhook (whsec_…) — a Stripe assina o corpo"),
    ("STRIPE_PUBLISHABLE_KEY", False, is_stripe_publishable,
     "chave publicável (pk_live_… em produção) — usada pelo Elements no browser"),

    # Numéricos com range razoável
    ("BLAXX_JWT_ACCESS_MIN", False, is_int_in_range(5, 1440),
     "min de TTL do access token (5..1440)"),
]


# ---------------------------------------------------------------------------- #
# API pública                                                                  #
# ---------------------------------------------------------------------------- #
def validate_env(*, strict: bool | None = None) -> list[str]:
    """Valida todas as envs do SCHEMA. Retorna lista de problemas detectados.

    `strict=True` → levanta EnvError no primeiro problema crítico (default em prod).
    `strict=False` → só retorna a lista, não levanta.
    `strict=None` (default) → strict em prod (FLASK_ENV != development/test).

    Em prod, env vars MAL setadas (placeholder) costumam ser PIORES que ausentes.
    A regra é: se o valor NÃO está vazio, ele precisa passar o validator.
    """
    if strict is None:
        strict = os.environ.get("FLASK_ENV", "production").lower() not in ("development", "test")

    issues: list[str] = []
    for name, required, validator, desc in SCHEMA:
        value = os.environ.get(name, "").strip()
        if not value:
            if required and strict:
                issues.append(f"[{name}] obrigatório em produção — {desc}")
            continue
        ok, hint = validator(value)
        if not ok:
            # Truncar value pra não vazar segredo se for SECRET_KEY com 32+
            shown = value if len(value) < 30 else value[:12] + "…" + value[-4:]
            issues.append(f"[{name}={shown!r}] inválido — {hint}")

    # ---- Obrigatoriedade condicional (só em prod/strict) ----
    if strict:
        # MercadoPago foi descontinuado (decisão 2026-08-01): PIX é Asaas,
        # cartão é Stripe. A exigência de MP_WEBHOOK_SECRET só permanece para
        # quem ainda estiver com PIX_PROVIDER=mercadopago durante a migração.
        if os.environ.get("PIX_PROVIDER", "").strip().lower() == "mercadopago":
            # AVISO, não bloqueio. Bloquear aqui tornaria a própria migração
            # impossível: não daria para deployar o código que habilita o
            # Asaas sem antes derrubar a produção, que ainda roda no MP.
            # O caminho correto é: deployar -> configurar Asaas no painel ->
            # trocar PIX_PROVIDER -> reiniciar.
            print(
                "[env_schema] AVISO: PIX_PROVIDER=mercadopago é LEGADO "
                "(descontinuado em 2026-08-01). Migrar para 'asaas' — o "
                "MercadoPago não paga PIX a terceiros, que é o que o resgate "
                "exige. Este aviso não bloqueia o boot.",
                file=sys.stderr,
            )
            if not os.environ.get("MP_WEBHOOK_SECRET", "").strip():
                issues.append(
                    "[MP_WEBHOOK_SECRET] obrigatório enquanto PIX_PROVIDER=mercadopago "
                    "— sem ela nenhum pagamento é confirmado"
                )
        # Asaas na entrada exige a key (o boot já levanta, mas aqui a mensagem
        # é clara e chega junto com os outros problemas de env).
        if os.environ.get("PIX_PROVIDER", "").strip().lower() == "asaas":
            if not os.environ.get("ASAAS_API_KEY", "").strip():
                issues.append(
                    "[ASAAS_API_KEY] obrigatório com PIX_PROVIDER=asaas"
                )
            if not os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip():
                issues.append(
                    "[ASAAS_WEBHOOK_TOKEN] obrigatório com PIX_PROVIDER=asaas — sem ele "
                    "o webhook de cobrança é rejeitado e nenhuma compra credita pontos"
                )
        # Stripe configurado exige o webhook secret: sem ele nenhum
        # pagamento internacional é confirmado.
        if os.environ.get("STRIPE_API_KEY", "").strip():
            if not os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip():
                issues.append(
                    "[STRIPE_WEBHOOK_SECRET] obrigatório com STRIPE_API_KEY — sem ele "
                    "o webhook é rejeitado e nenhuma compra internacional credita"
                )
            if not os.environ.get("STRIPE_API_KEY", "").startswith("sk_live_"):
                issues.append(
                    "[STRIPE_API_KEY] em produção precisa ser sk_live_… — "
                    "sk_test_ não cobra de verdade"
                )
        # Payout automático exige provider de saída configurado por inteiro.
        # Sem isso, o resgate falharia e estornaria — ou pior, ficaria retido
        # sem webhook que o confirme.
        if os.environ.get("PAYOUT_MODE", "").strip().lower() == "auto":
            if not os.environ.get("ASAAS_API_KEY", "").strip():
                issues.append(
                    "[ASAAS_API_KEY] obrigatório com PAYOUT_MODE=auto — sem provider "
                    "de saída o resgate não tem como pagar o usuário"
                )
            if not os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip():
                issues.append(
                    "[ASAAS_WEBHOOK_TOKEN] obrigatório com PAYOUT_MODE=auto — sem ele "
                    "o webhook é rejeitado e os payouts ficam retidos sem confirmação"
                )
            if os.environ.get("ASAAS_ENV", "sandbox").strip().lower() != "production":
                issues.append(
                    "[ASAAS_ENV] precisa ser 'production' com PAYOUT_MODE=auto em "
                    "produção — em sandbox o PIX do resgate NÃO é enviado de verdade"
                )
        # Cartão agora é Stripe (tem Elements → PAN não toca o backend).
        if os.environ.get("CARD_ENABLED", "").strip() == "1":
            for dep, why in (
                ("STRIPE_API_KEY", "processar o pagamento com cartão"),
                ("STRIPE_PUBLISHABLE_KEY", "o Elements tokenizar no browser"),
                ("STRIPE_WEBHOOK_SECRET", "confirmar o pagamento via webhook assinado"),
            ):
                if not os.environ.get(dep, "").strip():
                    issues.append(f"[{dep}] obrigatório com CARD_ENABLED=1 — {why}")

    if issues and strict:
        msg = "Env vars inválidas (recusando subir):\n  · " + "\n  · ".join(issues)
        # Imprime no stderr pra Render mostrar no Events tab
        print(msg, file=sys.stderr)
        raise EnvError(msg)

    return issues
