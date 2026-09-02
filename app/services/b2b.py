"""Emissão de pontos por redes parceiras (B2B).

O laço que este módulo fecha:

    cliente compra na rede  ->  rede chama POST /b2b/accrual
                            ->  pontos entram na carteira do cliente
                            ->  BlaXx registra um RECEBÍVEL contra a rede

A trava que sustenta o modelo
-----------------------------
Todo ponto emitido é passivo: mais cedo ou mais tarde alguém resgata por
`Config.CENTS_PER_POINT` centavos. Logo, a rede precisa pagar por ponto ao
menos esse tanto — senão cada emissão nasce com prejuízo, e o volume só piora
o resultado.

`_assert_contract_solvente` recusa a emissão quando o contrato está abaixo
disso. É deliberado que a recusa aconteça **na emissão**, e não só no cadastro:
o preço de resgate pode subir depois de o contrato ter sido assinado, e nesse
dia todo contrato defasado tem de parar de emitir, não continuar sangrando.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..config import Config
from ..extensions import db
from ..models import (
    InvoiceStatus, Merchant, MerchantAccrual, MerchantApiKey, MerchantInvoice,
    Transaction, TxType, User, Wallet,
)
from . import wallet as wallet_svc


class B2BError(Exception):
    """Erro de negócio devolvido como 4xx pela API."""

    def __init__(self, message: str, *, code: str = "b2b_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# Credenciais de máquina                                                       #
# --------------------------------------------------------------------------- #

KEY_PREFIX_LEN = 12


def issue_api_key(merchant: Merchant, *, label: str | None = None) -> tuple[MerchantApiKey, str]:
    """Cria uma chave e devolve (registro, segredo em claro).

    O segredo em claro só existe neste retorno — o banco guarda hash. Quem
    perder a chave gera outra; não há como recuperar a antiga.
    """
    prefix = secrets.token_hex(KEY_PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(32)
    raw = f"blx_{prefix}_{secret}"

    key = MerchantApiKey(merchant_id=merchant.id, prefix=prefix, label=label)
    key.set_key(raw)
    db.session.add(key)
    db.session.flush()
    return key, raw


def authenticate(raw_key: str | None) -> Merchant | None:
    """Resolve a chave para a rede dona, ou None.

    Busca pelo prefixo (indexado) e confere o hash do segredo inteiro. Chave
    revogada, inativa ou de rede inativa não autentica.
    """
    if not raw_key:
        return None
    parts = raw_key.split("_", 2)          # o segredo pode conter "_"
    if len(parts) != 3 or parts[0] != "blx":
        return None

    key = db.session.query(MerchantApiKey).filter_by(prefix=parts[1]).one_or_none()
    if key is None or not key.is_active or key.revoked_at is not None:
        return None
    if not key.check_key(raw_key):
        return None
    if key.merchant is None or not key.merchant.is_active:
        return None

    key.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return key.merchant


# --------------------------------------------------------------------------- #
# Identificação do cliente                                                     #
# --------------------------------------------------------------------------- #

def resolve_user(*, cpf: str | None = None, card_id: str | None = None) -> User:
    """Acha o cliente por CPF ou pelo número do Cartão BlaXx.

    O cartão é o prefixo de 8 hex do `user.id` — o mesmo que os apps mostram.
    Prefixo pode, em tese, colidir; duas linhas viram erro explícito em vez de
    creditar a pessoa errada.
    """
    if cpf:
        digits = re.sub(r"\D", "", cpf)
        if len(digits) != 11:
            raise B2BError("CPF inválido", code="invalid_cpf")
        user = db.session.query(User).filter_by(cpf=digits).one_or_none()
        if user is None:
            raise B2BError("Cliente não encontrado", code="customer_not_found", status=404)
        return user

    if card_id:
        token = re.sub(r"[^0-9a-fA-F]", "", card_id).lower()
        if len(token) != 8:
            raise B2BError("Cartão BlaXx inválido", code="invalid_card", status=400)
        found = (
            db.session.query(User)
            .filter(User.id.like(f"{token}%"))
            .limit(2)
            .all()
        )
        if not found:
            raise B2BError("Cliente não encontrado", code="customer_not_found", status=404)
        if len(found) > 1:
            raise B2BError(
                "Cartão ambíguo; peça o CPF do cliente",
                code="ambiguous_card", status=409,
            )
        return found[0]

    raise B2BError("Informe cpf ou card_id", code="missing_customer")


# --------------------------------------------------------------------------- #
# Emissão                                                                      #
# --------------------------------------------------------------------------- #

def _assert_contract_solvente(merchant: Merchant) -> None:
    piso = Config.CENTS_PER_POINT
    if merchant.bill_cents_per_point < piso:
        raise B2BError(
            "Contrato desta rede está abaixo do custo de resgate do ponto; "
            "emissão bloqueada até renegociação.",
            code="contract_below_redemption_cost",
            status=409,
        )


def compute_points(merchant: Merchant, amount_cents: int) -> int:
    """Pontos gerados por uma compra. Divisão inteira: resto não vira ponto."""
    if merchant.accrual_cents_per_point <= 0:
        raise B2BError("Regra de acúmulo inválida", code="invalid_accrual_rule")
    return amount_cents // merchant.accrual_cents_per_point


def award(
    merchant: Merchant,
    *,
    amount_cents: int,
    cpf: str | None = None,
    card_id: str | None = None,
    store_code: str | None = None,
    idempotency_key: str | None = None,
) -> MerchantAccrual:
    """Registra a compra, credita o cliente e grava o recebível.

    Idempotente por (merchant, idempotency_key): o PDV repete a chamada quando
    a rede cai no meio, e repetir não pode creditar duas vezes.
    """
    if amount_cents <= 0:
        raise B2BError("amount_cents deve ser positivo", code="invalid_amount")

    # 1) Replay antes de qualquer escrita.
    if idempotency_key:
        anterior = (
            db.session.query(MerchantAccrual)
            .filter_by(merchant_id=merchant.id, idempotency_key=idempotency_key)
            .one_or_none()
        )
        if anterior is not None:
            return anterior

    _assert_contract_solvente(merchant)

    user = resolve_user(cpf=cpf, card_id=card_id)
    if getattr(user, "is_deleted", False):
        raise B2BError("Cliente não encontrado", code="customer_not_found", status=404)

    points = compute_points(merchant, amount_cents)
    if points <= 0:
        raise B2BError(
            f"Compra abaixo do mínimo para pontuar ({merchant.accrual_label()})",
            code="below_minimum",
        )
    if points > merchant.max_points_per_tx:
        raise B2BError(
            f"Acima do teto por transação ({merchant.max_points_per_tx} pts)",
            code="above_tx_cap", status=409,
        )

    bill_cents = points * merchant.bill_cents_per_point

    accrual = MerchantAccrual(
        merchant_id=merchant.id,
        user_id=user.id,
        store_code=store_code,
        amount_cents=amount_cents,
        points_awarded=points,
        bill_cents=bill_cents,
        idempotency_key=idempotency_key,
    )
    db.session.add(accrual)
    db.session.flush()

    # 2) Crédito no ledger. A chave de idempotência da carteira deriva do id do
    # accrual, então mesmo um retry que passe pela checagem acima não duplica.
    tx = wallet_svc.credit(
        user_id=user.id,
        amount_pts=points,
        tx_type=TxType.ACCRUAL,
        description=f"Compra em {merchant.name}",
        reference=accrual.id,
        idempotency_key=f"accrual:{accrual.id}",
    )
    accrual.transaction_id = tx.id
    db.session.flush()
    return accrual


# --------------------------------------------------------------------------- #
# Fatura                                                                       #
# --------------------------------------------------------------------------- #

def statement(merchant: Merchant, *, since: datetime | None = None,
              until: datetime | None = None) -> dict:
    """Consolida o que a rede deve no período.

    Somatório do recebível já congelado em cada linha — não recalcula pelo
    preço atual do contrato, senão renegociar mudaria faturas passadas.
    """
    stmt = select(
        func.count(MerchantAccrual.id),
        func.coalesce(func.sum(MerchantAccrual.points_awarded), 0),
        func.coalesce(func.sum(MerchantAccrual.bill_cents), 0),
        func.coalesce(func.sum(MerchantAccrual.amount_cents), 0),
        func.coalesce(func.sum(MerchantAccrual.credit_cents), 0),
        func.coalesce(func.sum(MerchantAccrual.reversed_points), 0),
    ).where(MerchantAccrual.merchant_id == merchant.id)
    if since is not None:
        stmt = stmt.where(MerchantAccrual.created_at >= since)
    if until is not None:
        stmt = stmt.where(MerchantAccrual.created_at < until)

    count, pts, bill, gmv, credito, estornados = db.session.execute(stmt).one()
    return {
        "merchant": merchant.name,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "transactions": int(count),
        "points_issued": int(pts),
        "points_reversed": int(estornados),
        "gmv_brl": round(int(gmv) / 100, 2),
        "gross_brl": round(int(bill) / 100, 2),
        "credit_brl": round(int(credito) / 100, 2),
        # Líquido: o que a rede deve depois dos estornos. É este o número que
        # a UI mostra como "a pagar".
        "amount_due_brl": round((int(bill) - int(credito)) / 100, 2),
    }


# --------------------------------------------------------------------------- #
# Painel: séries e agregados                                                   #
# --------------------------------------------------------------------------- #

def daily_series(merchant: Merchant, *, days: int = 30) -> list[dict]:
    """Pontos, GMV e recebível por dia, do mais antigo ao mais novo.

    Preenche dias sem movimento com zero — um gráfico que pula datas mente
    sobre a frequência da rede.
    """
    from datetime import timedelta

    hoje = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                              second=0, microsecond=0)
    inicio = hoje - timedelta(days=days - 1)

    linhas = db.session.execute(
        select(
            func.date(MerchantAccrual.created_at).label("dia"),
            func.count(MerchantAccrual.id),
            func.coalesce(func.sum(MerchantAccrual.points_awarded), 0),
            func.coalesce(func.sum(MerchantAccrual.amount_cents), 0),
            func.coalesce(func.sum(MerchantAccrual.bill_cents), 0),
        )
        .where(MerchantAccrual.merchant_id == merchant.id)
        .where(MerchantAccrual.created_at >= inicio)
        .group_by(func.date(MerchantAccrual.created_at))
    ).all()

    porto_dia = {str(r[0]): r for r in linhas}
    saida = []
    for i in range(days):
        d = inicio + timedelta(days=i)
        chave = d.strftime("%Y-%m-%d")
        r = porto_dia.get(chave)
        saida.append({
            "date": chave,
            "transactions": int(r[1]) if r else 0,
            "points": int(r[2]) if r else 0,
            "gmv_brl": round(int(r[3]) / 100, 2) if r else 0.0,
            "bill_brl": round(int(r[4]) / 100, 2) if r else 0.0,
        })
    return saida


def panel_summary(merchant: Merchant) -> dict:
    """Números do topo do painel: hoje, 30 dias e total."""
    from datetime import timedelta

    hoje = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                              second=0, microsecond=0)
    return {
        "merchant": merchant.to_dict(),
        "today": statement(merchant, since=hoje),
        "last_30d": statement(merchant, since=hoje - timedelta(days=29)),
        "all_time": statement(merchant),
        "series": daily_series(merchant, days=30),
        # Quantos clientes distintos a rede pontuou — mede alcance do programa,
        # não volume. Rede com muito ponto e poucos clientes é caixa único.
        "customers_30d": int(db.session.execute(
            select(func.count(func.distinct(MerchantAccrual.user_id)))
            .where(MerchantAccrual.merchant_id == merchant.id)
            .where(MerchantAccrual.created_at >= hoje - timedelta(days=29))
        ).scalar() or 0),
    }


# --------------------------------------------------------------------------- #
# Estorno — venda cancelada no PDV                                             #
# --------------------------------------------------------------------------- #

def reverse(
    merchant: Merchant,
    *,
    accrual_id: str | None = None,
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> MerchantAccrual:
    """Desfaz um acúmulo. Idempotente: estornar duas vezes não debita de novo.

    O caso difícil, e a razão de este código não ser um `UPDATE ... SET`:
    **o cliente pode já ter gastado os pontos.** Quando isso acontece não há o
    que retirar, e alguém tem de arcar. A regra aqui:

      · pontos ainda na carteira  -> retirados, e a rede é creditada por eles
      · pontos já gastos          -> não há clawback, e a rede **continua
                                     devendo** por essa parte

    O motivo de a rede continuar devendo o resto é que o valor já foi entregue
    ao cliente em nome dela. Jogar essa perda na BlaXx transformaria "cancelar
    a venda depois do resgate" num jeito de sacar dinheiro de graça.

    A resposta diz quantos pontos voltaram e quantos não voltaram — o operador
    do PDV precisa ver a diferença, não descobrir na fatura.
    """
    q = db.session.query(MerchantAccrual).filter_by(merchant_id=merchant.id)
    if accrual_id:
        registro = q.filter_by(id=accrual_id).one_or_none()
    elif idempotency_key:
        registro = q.filter_by(idempotency_key=idempotency_key).one_or_none()
    else:
        raise B2BError("informe accrual_id ou idempotency_key", code="missing_ref")

    if registro is None:
        raise B2BError("lançamento não encontrado", code="accrual_not_found", status=404)
    if registro.is_reversed:
        return registro                      # idempotente

    wallet = db.session.query(Wallet).filter_by(user_id=registro.user_id).one_or_none()
    saldo = wallet.balance_pts if wallet is not None else 0
    recuperaveis = max(0, min(registro.points_awarded, saldo))

    tx = None
    if recuperaveis > 0:
        tx = wallet_svc.debit(
            user_id=registro.user_id,
            amount_pts=recuperaveis,
            tx_type=TxType.REFUND,
            description=f"Estorno de compra em {merchant.name}",
            reference=registro.id,
            idempotency_key=f"accrual-reversal:{registro.id}",
        )

    # Preço por ponto congelado na emissão: `bill_cents` sempre é múltiplo
    # exato de `points_awarded`, então a divisão inteira é exata.
    por_ponto = (registro.bill_cents // registro.points_awarded) if registro.points_awarded else 0

    registro.reversed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    registro.reversed_points = recuperaveis
    registro.credit_cents = recuperaveis * por_ponto
    registro.reversal_transaction_id = tx.id if tx is not None else None
    registro.reversal_reason = (reason or "cancelamento no PDV")[:200]
    db.session.flush()
    return registro


# --------------------------------------------------------------------------- #
# Fatura                                                                       #
# --------------------------------------------------------------------------- #

def _numero_fatura(merchant: Merchant, fim: datetime) -> str:
    seq = db.session.query(func.count(MerchantInvoice.id)).filter_by(
        merchant_id=merchant.id).scalar() or 0
    return f"BLX-{merchant.vertical.value[:3].upper()}-{fim.strftime('%Y%m')}-{seq + 1:03d}"


def close_invoice(
    merchant: Merchant,
    *,
    period_start: datetime,
    period_end: datetime,
    due_date: datetime | None = None,
) -> MerchantInvoice:
    """Fecha o período e congela os totais.

    Junta duas coisas distintas:

      1. COBRANÇAS — acúmulos do período ainda sem fatura
      2. CRÉDITOS  — estornos ainda não devolvidos, **de qualquer período**

    Os créditos entram sem filtro de data de propósito: um estorno de uma venda
    de agosto que chegou em setembro tem de aparecer na fatura de setembro. Se
    filtrasse por período, esse crédito nunca seria devolvido.

    Nada é recalculado depois. Fatura fechada é imutável — exceto anular, que
    devolve as linhas para o estado em aberto.
    """
    if period_end <= period_start:
        raise B2BError("período inválido", code="invalid_period")

    cobrancas = (
        db.session.query(MerchantAccrual)
        .filter_by(merchant_id=merchant.id, invoice_id=None)
        .filter(MerchantAccrual.created_at >= period_start)
        .filter(MerchantAccrual.created_at < period_end)
        .all()
    )
    creditos = (
        db.session.query(MerchantAccrual)
        .filter_by(merchant_id=merchant.id, credit_invoice_id=None)
        .filter(MerchantAccrual.reversed_at.isnot(None))
        .filter(MerchantAccrual.credit_cents > 0)
        .all()
    )
    if not cobrancas and not creditos:
        raise B2BError("nada a faturar neste período", code="nothing_to_invoice")

    fatura = MerchantInvoice(
        merchant_id=merchant.id,
        number=_numero_fatura(merchant, period_end),
        period_start=period_start,
        period_end=period_end,
        due_date=due_date,
        transactions=len(cobrancas),
        points_issued=sum(a.points_awarded for a in cobrancas),
        points_reversed=sum(a.reversed_points for a in creditos),
        gmv_cents=sum(a.amount_cents for a in cobrancas),
        gross_cents=sum(a.bill_cents for a in cobrancas),
        credit_cents=sum(a.credit_cents for a in creditos),
    )
    fatura.amount_cents = fatura.gross_cents - fatura.credit_cents
    db.session.add(fatura)
    db.session.flush()

    for a in cobrancas:
        a.invoice_id = fatura.id
    for a in creditos:
        a.credit_invoice_id = fatura.id
    db.session.flush()
    return fatura


def mark_invoice_paid(fatura: MerchantInvoice, *, note: str | None = None) -> MerchantInvoice:
    if fatura.status is InvoiceStatus.VOID:
        raise B2BError("fatura anulada não pode ser paga", code="invoice_void", status=409)
    if fatura.status is InvoiceStatus.PAID:
        return fatura                        # idempotente
    fatura.status = InvoiceStatus.PAID
    fatura.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
    fatura.payment_note = (note or None)
    db.session.flush()
    return fatura


def void_invoice(fatura: MerchantInvoice, *, note: str | None = None) -> MerchantInvoice:
    """Anula a fatura e SOLTA as linhas de volta para o estado em aberto.

    Anular é para fechamento feito por engano. Se as linhas continuassem
    carimbadas, elas nunca mais seriam cobradas — some receita em silêncio.
    """
    if fatura.status is InvoiceStatus.PAID:
        raise B2BError("fatura já paga não pode ser anulada", code="invoice_paid", status=409)

    db.session.query(MerchantAccrual).filter_by(invoice_id=fatura.id).update(
        {"invoice_id": None}, synchronize_session=False)
    db.session.query(MerchantAccrual).filter_by(credit_invoice_id=fatura.id).update(
        {"credit_invoice_id": None}, synchronize_session=False)

    fatura.status = InvoiceStatus.VOID
    fatura.payment_note = (note or "anulada")[:200]
    db.session.flush()
    return fatura


def open_balance(merchant: Merchant) -> dict:
    """O que ainda NÃO entrou em fatura — a próxima cobrança."""
    cob = db.session.execute(
        select(func.count(MerchantAccrual.id),
               func.coalesce(func.sum(MerchantAccrual.bill_cents), 0))
        .where(MerchantAccrual.merchant_id == merchant.id)
        .where(MerchantAccrual.invoice_id.is_(None))
    ).one()
    cred = db.session.execute(
        select(func.coalesce(func.sum(MerchantAccrual.credit_cents), 0))
        .where(MerchantAccrual.merchant_id == merchant.id)
        .where(MerchantAccrual.credit_invoice_id.is_(None))
        .where(MerchantAccrual.reversed_at.isnot(None))
    ).scalar() or 0
    bruto = int(cob[1])
    return {
        "transactions": int(cob[0]),
        "gross_brl": round(bruto / 100, 2),
        "credit_brl": round(int(cred) / 100, 2),
        "amount_brl": round((bruto - int(cred)) / 100, 2),
    }


def unpaid_total_cents(merchant: Merchant) -> int:
    """Faturas fechadas e ainda não pagas."""
    return int(db.session.execute(
        select(func.coalesce(func.sum(MerchantInvoice.amount_cents), 0))
        .where(MerchantInvoice.merchant_id == merchant.id)
        .where(MerchantInvoice.status == InvoiceStatus.OPEN)
    ).scalar() or 0)
