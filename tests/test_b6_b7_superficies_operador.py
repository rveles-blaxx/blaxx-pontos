"""B-6/B-7: /metrics/health e /docs descreviam a instalacao para qualquer um.

Nenhum dos dois expoe dado de cliente. Os dois expoem a INSTALACAO: versao do
release, provedores contratados, se ha Sentry, a classe da excecao quando o
banco cai — e, no /docs, o mapa completo da API com todo parametro aceito,
inclusive das rotas de admin e payout. Junto, e o reconhecimento que se faz
antes de escolher por onde atacar.

Detalhe que estes testes existem para travar: o payload PUBLICO do
/metrics/health precisa continuar contendo `uptime_s`, `db` e `providers.pix`.
E deles que o deploy_guard.py depende para provar que um deploy chegou — foi
essa checagem que diagnosticou o corte de gateway. Fechar o endpoint inteiro
trocaria uma exposicao pequena pela cegueira em producao.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def prod(app, monkeypatch):
    """Finge producao: em dev/teste as superficies de operador ficam abertas.

    Nao adianta apagar PYTEST_CURRENT_TEST — o pytest reescreve a variavel no
    inicio de cada fase do teste, entao o gate reabriria antes da requisicao e
    o teste passaria vazio, provando nada. Fechamos pela propria funcao.
    """
    monkeypatch.setattr("app.security.ambiente_local", lambda: False)
    return app.test_client()


def _basic(usuario: str, senha: str) -> dict:
    import base64

    cred = base64.b64encode(f"{usuario}:{senha}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


# ───────────────────────────────────────────────────────────────── B-6 ───────
def test_health_publico_nao_entrega_versao_nem_mailer_nem_sentry(prod):
    r = prod.get("/metrics/health")
    assert r.status_code == 200
    corpo = r.get_json()
    assert "version" not in corpo
    provedores = corpo["providers"]
    assert "mailer" not in provedores
    assert "sentry" not in provedores
    assert "push" not in provedores


def test_health_publico_preserva_o_que_o_deploy_guard_le(prod):
    corpo = prod.get("/metrics/health").get_json()
    assert isinstance(corpo["uptime_s"], int)
    assert corpo["db"] == "ok"
    assert corpo["providers"]["pix"], "deploy_guard --expect-pix depende disso"


def test_health_com_credencial_de_operador_entrega_tudo(prod, monkeypatch):
    monkeypatch.setenv("METRICS_USER", "operador")
    monkeypatch.setenv("METRICS_PASS", "segredo-de-teste")
    corpo = prod.get(
        "/metrics/health", headers=_basic("operador", "segredo-de-teste")
    ).get_json()
    assert "version" in corpo
    assert "mailer" in corpo["providers"]
    assert "sentry" in corpo["providers"]


def test_credencial_errada_cai_no_payload_publico(prod, monkeypatch):
    monkeypatch.setenv("METRICS_USER", "operador")
    monkeypatch.setenv("METRICS_PASS", "segredo-de-teste")
    corpo = prod.get(
        "/metrics/health", headers=_basic("operador", "chute")
    ).get_json()
    assert "version" not in corpo


# ───────────────────────────────────────────────────────────────── B-7 ───────
@pytest.mark.parametrize("rota", ["/docs/", "/docs/openapi.yaml"])
def test_docs_exige_operador(prod, rota):
    r = prod.get(rota)
    assert r.status_code == 401
    assert "Basic" in r.headers.get("WWW-Authenticate", "")


def test_docs_abre_com_credencial(prod, monkeypatch):
    monkeypatch.setenv("METRICS_USER", "operador")
    monkeypatch.setenv("METRICS_PASS", "segredo-de-teste")
    r = prod.get("/docs/", headers=_basic("operador", "segredo-de-teste"))
    assert r.status_code == 200


def test_sem_credencial_configurada_ninguem_entra(prod, monkeypatch):
    """Default seguro: instalacao que esqueceu as env vars fica fechada."""
    monkeypatch.delenv("METRICS_USER", raising=False)
    monkeypatch.delenv("METRICS_PASS", raising=False)
    assert prod.get("/docs/").status_code == 401
    assert prod.get(
        "/docs/", headers=_basic("qualquer", "coisa")
    ).status_code == 401


def test_docs_seguem_abertos_em_dev(app):
    """DX: exigir senha na suite so faria alguem desligar a verificacao."""
    assert app.test_client().get("/docs/").status_code == 200
