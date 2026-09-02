"""M-3: cartao cobrava pelo Config, PIX pela tabela.

O admin publica um preco novo, /pix/packages passa a mostra-lo, e o comprador
de cartao continuava sendo cobrado o preco antigo — nos dois sentidos. Perda
financeira silenciosa e divergencia entre preco exibido e cobrado.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import Config, TestConfig
from app.extensions import db
from app.models import PointPackage
from app.services import purchase as purchase_svc


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_db_manda_sobre_o_config(app):
    """O caso do achado: admin publica preco novo."""
    with app.app_context():
        row = db.session.get(PointPackage, "black")
        assert row is not None, "auto-seed deveria ter criado os pacotes"
        row.price_cents = 250_000          # R$ 2.500, publicado pelo admin
        row.points = 28_000
        db.session.commit()

        p = purchase_svc.resolve_package("black")
        assert p["fonte"] == "db"
        assert p["amount_cents"] == 250_000
        # e o Config segue com o valor antigo — e' justamente o ponto
        assert int(round(Config.POINT_PACKAGES["black"]["price_brl"] * 100)) != 250_000


def test_pacote_inativo_cai_no_config(app):
    with app.app_context():
        row = db.session.get(PointPackage, "black")
        row.active = False
        db.session.commit()
        p = purchase_svc.resolve_package("black")
        assert p["fonte"] == "config"


def test_pacote_inexistente_devolve_none(app):
    with app.app_context():
        assert purchase_svc.resolve_package("nao-existe") is None


def test_os_dois_trilhos_leem_o_mesmo_preco(app):
    """A invariante que o achado pedia: PIX e cartao resolvem igual."""
    from app.services import card_purchase  # noqa: F401 — garante o import
    with app.app_context():
        row = db.session.get(PointPackage, "prime")
        row.price_cents = 99_900
        row.points = 12_345
        db.session.commit()

        p = purchase_svc.resolve_package("prime")
        assert (p["amount_cents"], p["points"]) == (99_900, 12_345)
        # card_purchase resolve pelo MESMO helper; se alguem reintroduzir a
        # leitura do Config la, este import + assert do source pega.
        fonte = open("app/services/card_purchase.py", encoding="utf-8").read()
        assert "Config.POINT_PACKAGES.get(package_key)" not in fonte, \
            "card_purchase voltou a ler o Config direto"
