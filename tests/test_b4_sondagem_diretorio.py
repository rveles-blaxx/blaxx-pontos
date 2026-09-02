"""B-4: /transfer confirmava, uma consulta por vez, quem e cliente da BlaXx.

"destinatario nao encontrado" e uma resposta necessaria — quem digita errado
precisa saber. Mas ela tambem responde a pergunta inversa: dado um CPF ou
e-mail, essa pessoa tem conta aqui? Com uma conta valida, uma lista de CPFs
virava uma lista de clientes, sem tocar em saldo (a resolucao do destinatario
acontece antes de qualquer checagem de valor).

O controle nao esconde a mensagem — limita ALVOS DISTINTOS por hora.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, User, Wallet

SENHA = "StrongP@ss1!"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk(app, email, cpf, saldo=0):
    with app.app_context():
        u = User(name="U Silva", email=email, cpf=cpf, pix_key=email)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=saldo))
        db.session.commit()
        return u.id


def _sondar(client, tok, alvo):
    return client.post(
        "/transfer/",
        json={"to": alvo, "amount_pts": 500, "password": SENHA},
        headers={"Authorization": f"Bearer {tok}"},
    )


def _token(client):
    return client.post(
        "/auth/login", json={"email": "a@x.test", "password": SENHA}
    ).get_json()["token"]


def test_varredura_de_alvos_distintos_e_barrada_com_429(app, client):
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    tok = _token(client)

    # Os 5 primeiros alvos distintos ainda respondem com a mensagem util.
    for i in range(5):
        r = _sondar(client, tok, f"ninguem{i}@x.test")
        assert r.status_code == 400, f"alvo {i} deveria seguir respondendo"
        assert "não encontrado" in r.get_json()["error"]

    # O sexto e a varredura: recua sem confirmar nada sobre o alvo.
    r = _sondar(client, tok, "ninguem5@x.test")
    assert r.status_code == 429
    corpo = r.get_json()
    assert corpo["code"] == "RECIPIENT_PROBE_BLOCKED"
    assert "não encontrado" not in corpo["error"], (
        "a resposta de bloqueio nao pode continuar confirmando o alvo"
    )


def test_errar_o_mesmo_alvo_varias_vezes_nao_bloqueia(app, client):
    """O caso legitimo: um e-mail digitado errado, repetido.

    Se o controle contasse TENTATIVAS em vez de alvos distintos, ele puniria
    exatamente quem esta tentando acertar um endereco so.
    """
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    tok = _token(client)

    for _ in range(8):
        r = _sondar(client, tok, "amigo.com.typo@x.test")
        assert r.status_code == 400, "mesmo alvo repetido nao e varredura"


def test_auditoria_guarda_hash_e_nunca_o_identificador(app, client):
    """A trilha nao pode virar o diretorio que este controle protege."""
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    tok = _token(client)
    _sondar(client, tok, "vitima@empresa.test")
    _sondar(client, tok, "11144477735")

    with app.app_context():
        registros = (
            db.session.query(AuditLog)
            .filter(AuditLog.event == "transfer_recipient_miss")
            .all()
        )
        assert len(registros) == 2
        for reg in registros:
            assert "vitima@empresa.test" not in (reg.extra_data or "")
            assert "11144477735" not in (reg.extra_data or "")
            assert '"alvo"' in (reg.extra_data or "")


def test_destinatario_valido_nao_conta_como_sondagem(app, client):
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    _mk(app, "b@x.test", "15350946056")
    tok = _token(client)

    r = _sondar(client, tok, "b@x.test")
    assert r.status_code == 201

    with app.app_context():
        assert (
            db.session.query(AuditLog)
            .filter(AuditLog.event == "transfer_recipient_miss")
            .count()
            == 0
        )
