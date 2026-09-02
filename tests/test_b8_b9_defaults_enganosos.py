"""B-8/B-9: dois defaults do Config que respondiam com confianca errada.

B-8 — GOOGLE_*_CLIENT_ID caiam num valor embutido no codigo. Client_id nao e
segredo; o problema e o fallback ser MUDO. Trocado o projeto Google e esquecida
a env var, /auth/google segue validando contra o projeto antigo, funcionando o
bastante para ninguem investigar. O default fica (o app iOS publicado depende
do audience dele — remover derruba o login de quem ja instalou), mas o boot
grita.

B-9 — Config.PAYOUT_MODE default-ava "auto" e, por ser atributo de Config,
virava app.config["PAYOUT_MODE"]. O factory forca "manual" quando o provider
nao sabe pagar de verdade; quem lesse a chave antiga concluiria que o resgate
paga sozinho num app que na verdade tem fila manual.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import Config, TestConfig
from app.extensions import db


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


# ───────────────────────────────────────────────────────────────── B-9 ───────
def test_payout_mode_ambiguo_nao_existe_mais(app):
    assert not hasattr(Config, "PAYOUT_MODE"), (
        "o atributo voltou — e com ele a chance de app.config['PAYOUT_MODE'] "
        "discordar do modo realmente em vigor"
    )
    assert "PAYOUT_MODE" not in app.config


def test_modo_efetivo_e_a_fonte_unica(app):
    assert app.config["PAYOUT_MODE_EFFECTIVE"] in ("auto", "manual")


# ───────────────────────────────────────────────────────────────── B-8 ───────
def test_client_id_no_default_e_declarado(monkeypatch):
    """Sem env var, o Config precisa admitir que esta usando o embutido."""
    import importlib

    for var in ("GOOGLE_WEB_CLIENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_IOS_CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)
    import app.config as cfg

    importlib.reload(cfg)
    assert set(cfg.Config.GOOGLE_CLIENT_IDS_EM_DEFAULT) == {
        "GOOGLE_WEB_CLIENT_ID",
        "GOOGLE_IOS_CLIENT_ID",
    }
    # E o fallback continua valendo: apagar quebraria o app iOS ja publicado.
    assert cfg.Config.google_allowed_audiences(), "audiences nao podem sumir"
    importlib.reload(cfg)


def test_env_var_setada_some_da_lista(monkeypatch):
    import importlib

    monkeypatch.setenv("GOOGLE_WEB_CLIENT_ID", "web-de-teste.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_IOS_CLIENT_ID", "ios-de-teste.apps.googleusercontent.com")
    import app.config as cfg

    importlib.reload(cfg)
    assert cfg.Config.GOOGLE_CLIENT_IDS_EM_DEFAULT == []
    assert "web-de-teste.apps.googleusercontent.com" in cfg.Config.google_allowed_audiences()
    monkeypatch.undo()
    importlib.reload(cfg)


def test_boot_em_producao_avisa_sobre_o_client_id_embutido(caplog, monkeypatch):
    """O aviso e a correcao inteira do B-8 — se ele nao sai, nada mudou."""
    import logging

    monkeypatch.setattr(
        Config, "GOOGLE_CLIENT_IDS_EM_DEFAULT", ["GOOGLE_IOS_CLIENT_ID"], raising=False
    )

    class ConfigProd(TestConfig):
        # O factory se recusa a subir "em producao" com segredo default — e
        # bom que se recuse. Damos segredos de verdade em vez de afrouxar o
        # guard: desligar a salvaguarda para o teste passar transformaria o
        # teste num aviso de que a salvaguarda existe, nao de que ela funciona.
        TESTING = False
        DEBUG = False
        SECRET_KEY = "chave-de-teste-nao-default-0123456789abcdef"
        JWT_SECRET_KEY = "jwt-de-teste-nao-default-0123456789abcdef"

    with caplog.at_level(logging.WARNING):
        create_app(ConfigProd)
    assert any(
        "client_id embutido" in r.getMessage() for r in caplog.records
    ), "producao subiu com o default do Google sem avisar ninguem"
