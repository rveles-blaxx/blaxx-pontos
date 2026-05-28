"""Sprint 5 (S5-6) · A/B testing framework leve.

Assignment deterministico (hash user_id + experiment_key) — mesmo usuario
SEMPRE recebe a mesma variante, sem precisar persistir nada. Quando os
dados de conversao acumularem, o teste pode ser analisado offline pelos
audit_logs do tipo "experiment_exposure" e "experiment_conversion".

Sem libs externas — usa stdlib `hashlib`.

Convenções:
  - experiment_key: snake_case curto. Ex: "pkg_pricing_v2", "btn_color"
  - variants: lista de strings. Ex: ["control", "treatment"]
  - weight: opcional. Ex: [50, 50] | [70, 30] (precisa somar 100)

Usage no codigo:
    from app.services.experiments import get_variant
    variant = get_variant(user, "pkg_pricing_v2", ["control", "treatment"])
    if variant == "treatment":
        price = 470 * 0.85  # 15% off
    log_exposure(user, "pkg_pricing_v2", variant)
    # ... mais tarde, ao confirmar compra:
    log_conversion(user, "pkg_pricing_v2", variant, value=price)
"""

from __future__ import annotations

import hashlib
from typing import Sequence


def _bucket(user_id: str, experiment_key: str, n: int = 100) -> int:
    """Hash deterministico → bucket [0..n).

    SHA-256 dos primeiros 8 bytes — distribuicao uniforme suficiente
    pra populacoes >100k.
    """
    digest = hashlib.sha256(f"{user_id}:{experiment_key}".encode()).digest()
    # 4 primeiros bytes como int unsigned big-endian
    val = int.from_bytes(digest[:4], "big")
    return val % n


def get_variant(
    user,
    experiment_key: str,
    variants: Sequence[str],
    weights: Sequence[int] | None = None,
) -> str:
    """Devolve a variante consistente pra esse user nesse experimento.

    weights opcional — se None, distribui igualmente.
    Se user e' None ou nao tem id, retorna sempre a primeira variante
    (control) — anonimos ficam fora do teste.
    """
    if not variants:
        raise ValueError("variants vazio")
    if not user or not getattr(user, "id", None):
        return variants[0]

    if weights is None:
        # Distribuicao igual
        weights = [100 // len(variants)] * len(variants)
        # Ajusta resto pra somar 100
        weights[0] += 100 - sum(weights)

    if sum(weights) != 100:
        raise ValueError(f"weights precisa somar 100, recebido {sum(weights)}")
    if len(weights) != len(variants):
        raise ValueError("len(weights) != len(variants)")

    b = _bucket(user.id, experiment_key, n=100)
    cumulative = 0
    for w, v in zip(weights, variants):
        cumulative += w
        if b < cumulative:
            return v
    # Defensivo: nao deveria chegar aqui
    return variants[-1]


def log_exposure(user, experiment_key: str, variant: str) -> None:
    """Registra que o user foi exposto a essa variante (analytics).

    Usa audit_logs como sink — depois um job extrai pra DataDog/BigQuery
    pra calcular conversao. Idempotente por (user_id, experiment_key) —
    so loga 1 exposicao por user por experimento (evita inflacionar).
    """
    if not user or not getattr(user, "id", None):
        return
    try:
        from .audit import log_event
        log_event(
            "experiment_exposure",
            user_id=user.id,
            extra={"experiment": experiment_key, "variant": variant},
        )
    except Exception:
        pass


def log_conversion(user, experiment_key: str, variant: str,
                   value: float | None = None,
                   event: str = "conversion") -> None:
    """Registra que o user converteu nesse experimento.

    value: opcional. Ex: valor em R$ da compra, num pts resgatados.
    event: tipo de conversao. Ex: "purchase", "signup", "redeem".
    """
    if not user or not getattr(user, "id", None):
        return
    try:
        from .audit import log_event
        extra = {"experiment": experiment_key, "variant": variant, "event": event}
        if value is not None:
            extra["value"] = float(value)
        log_event("experiment_conversion", user_id=user.id, extra=extra)
    except Exception:
        pass


# ============================================================================
# Experimentos atualmente rodando (registro central pra evitar key collision)
# ============================================================================

REGISTERED_EXPERIMENTS = {
    # exemplo:
    # "pkg_pricing_v2": {
    #     "description": "15% off em Plus/Prime/Black",
    #     "variants": ["control", "treatment"],
    #     "weights": [50, 50],
    #     "started_at": "2026-06-01",
    #     "owner": "growth@blaxx",
    # },
}


def list_active() -> list[dict]:
    """Lista experimentos ativos pro endpoint /experiments/."""
    return [
        {"key": k, **v} for k, v in REGISTERED_EXPERIMENTS.items()
    ]
