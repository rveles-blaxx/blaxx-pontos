"""Envio de pontos entre clientes inscritos (P2P interno).

Regras (espelham as telas enviar-pontos.html / confirmar-envio.html):
  * Mínimo 100 pts, máximo 50.000 pts/dia (somando todos os envios).
  * Sem taxa.
  * Identifica destinatário por e-mail OU CPF.
  * Exige senha do remetente para confirmar.
  * Não pode enviar para si mesmo.
  * Gera receipt_code (ENV-2026-XXXX-XXXX) — comprovante.
  * Atômico: se algo falhar, nem débito nem crédito acontecem.
"""

from __future__ import annotations

import re

from ..config import Config
from ..extensions import db
from ..models import Transfer, TxType, User
from . import wallet as wallet_svc


class TransferError(Exception):
    pass


_CPF_RE = re.compile(r"\D+")


def _normalize_cpf(value: str) -> str:
    return _CPF_RE.sub("", value)


def find_recipient(identifier: str) -> User | None:
    """Aceita e-mail OU CPF (com ou sem máscara)."""
    identifier = (identifier or "").strip().lower()
    if "@" in identifier:
        return db.session.query(User).filter_by(email=identifier).one_or_none()
    cpf = _normalize_cpf(identifier)
    if not cpf:
        return None
    return db.session.query(User).filter_by(cpf=cpf).one_or_none()


def send(
    sender: User,
    *,
    recipient_identifier: str,
    amount_pts: int,
    password: str,
    message: str | None = None,
) -> Transfer:
    if not sender.check_password(password):
        raise TransferError("senha incorreta")

    if amount_pts < Config.TRANSFER_MIN_POINTS:
        raise TransferError(
            f"valor mínimo é {Config.TRANSFER_MIN_POINTS} pts"
        )

    recipient = find_recipient(recipient_identifier)
    if recipient is None:
        raise TransferError("destinatário não encontrado")

    if recipient.id == sender.id:
        raise TransferError("não é possível enviar pontos para si mesmo")

    # VIP: usuários marcados pelo admin podem transferir sem limite diário.
    if not sender.is_vip:
        sent_today = wallet_svc.debited_today(sender.id, TxType.TRANSFER_OUT)
        if sent_today + amount_pts > Config.TRANSFER_MAX_POINTS_PER_DAY:
            remaining = Config.TRANSFER_MAX_POINTS_PER_DAY - sent_today
            raise TransferError(
                f"limite diário excedido — restam {max(remaining,0)} pts hoje"
            )

    if message and len(message) > 140:
        raise TransferError("mensagem com mais de 140 caracteres")

    transfer = Transfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        amount_pts=amount_pts,
        message=message,
        receipt_code=Transfer.make_receipt(),
    )
    db.session.add(transfer)
    db.session.flush()

    # Débito + crédito atômicos: se algo falhar, rollback derruba os dois
    try:
        wallet_svc.debit(
            user_id=sender.id,
            amount_pts=amount_pts,
            tx_type=TxType.TRANSFER_OUT,
            description=f"Envio para {recipient.name}",
            reference=transfer.id,
            idempotency_key=f"transfer-out:{transfer.id}",
        )
        wallet_svc.credit(
            user_id=recipient.id,
            amount_pts=amount_pts,
            tx_type=TxType.TRANSFER_IN,
            description=f"Recebido de {sender.name}",
            reference=transfer.id,
            idempotency_key=f"transfer-in:{transfer.id}",
        )
    except wallet_svc.InsufficientBalance as exc:
        db.session.rollback()
        raise TransferError(str(exc)) from exc

    db.session.commit()
    return transfer
