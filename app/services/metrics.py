"""Sprint 5 (S5-4) · Métricas Prometheus customizadas.

Wrapper sobre `prometheus_client.Counter` que:
  * Não quebra se a lib não estiver instalada (no-op silencioso)
  * Permite reset entre testes via _reset_for_tests()
  * Mantém referência global única dos counters (evita "Duplicated timeseries")

Exposição: o endpoint `/metrics` é registrado em app/__init__.py via
PrometheusMetrics. Esses counters customizados são coletados automaticamente
pelo mesmo registry default.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


try:
    from prometheus_client import Counter
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False
    Counter = None  # type: ignore


_counters: dict[str, Any] = {}


def _get_counter(name: str, description: str, labels: tuple[str, ...]) -> Any | None:
    if not _PROM_AVAILABLE:
        return None
    if name in _counters:
        return _counters[name]
    try:
        c = Counter(name, description, labels)
        _counters[name] = c
        return c
    except ValueError:
        # "Duplicated timeseries" — outro import já criou. Busca no registry.
        try:
            from prometheus_client import REGISTRY
            for collector in list(REGISTRY._collector_to_names):
                names = REGISTRY._collector_to_names.get(collector, set())
                if name in names or f"{name}_total" in names:
                    _counters[name] = collector
                    return collector
        except Exception:
            pass
        return None


def inc_transfer(status: str) -> None:
    c = _get_counter("blaxx_transfer", "BlaXx transfers", ("status",))
    if c is not None:
        try:
            c.labels(status=status).inc()
        except Exception:
            pass


def inc_redeem(status: str) -> None:
    c = _get_counter("blaxx_redeem", "BlaXx redeems", ("status",))
    if c is not None:
        try:
            c.labels(status=status).inc()
        except Exception:
            pass


def inc_purchase(status: str, provider: str = "mock") -> None:
    c = _get_counter("blaxx_purchase", "BlaXx purchases (PIX charges)", ("status", "provider"))
    if c is not None:
        try:
            c.labels(status=status, provider=provider).inc()
        except Exception:
            pass


def inc_aml_alert(kind: str, severity: str) -> None:
    c = _get_counter("blaxx_aml_alerts", "BlaXx AML alerts", ("kind", "severity"))
    if c is not None:
        try:
            c.labels(kind=kind, severity=severity).inc()
        except Exception:
            pass


def inc_login(result: str) -> None:
    """result: 'success' | 'failed' | 'mfa_required' | 'locked'."""
    c = _get_counter("blaxx_login", "BlaXx login attempts", ("result",))
    if c is not None:
        try:
            c.labels(result=result).inc()
        except Exception:
            pass


def _reset_for_tests() -> None:
    """Limpa counters — só pra testes que precisam isolamento."""
    _counters.clear()
