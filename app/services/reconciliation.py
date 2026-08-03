"""Conciliação do ledger — confere se o saldo bate com a história.

POR QUE ESTE MÓDULO EXISTE
--------------------------
Pontos do BlaXx são passivo: cada um vale dinheiro no resgate. O saldo vive em
`Wallet.balance_pts` e a história em `Transaction`. Nada garante, em runtime,
que os dois concordem — um `commit` parcial, um retry mal isolado ou um bug de
serviço fazem o saldo divergir da soma do ledger, e **isso não gera erro**. O
usuário simplesmente passa a ter mais (ou menos) pontos do que deveria, e a
descoberta vem pela reclamação ou pelo prejuízo.

Esta rotina existe para transformar esse silêncio em alarme.

PRINCÍPIO: SÓ LÊ, NUNCA CORRIGE
-------------------------------
Nenhuma função aqui escreve no banco. Conciliação que "conserta sozinha"
mascara a causa e produz um segundo bug em cima do primeiro — some a evidência
de que algo esteve errado. O papel daqui é dizer o que divergiu, quanto, e em
qual carteira. A correção é decisão humana, feita com o achado em mãos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..extensions import db
from ..models import (
    PixCharge,
    PixChargeStatus,
    PixPayout,
    PixPayoutStatus,
    Transaction,
    TxStatus,
    TxType,
    Wallet,
)

# Só transação CONFIRMED move saldo. PENDING ainda não aconteceu e REVERSED foi
# desfeita — somar qualquer uma das duas produziria divergência falsa, que é o
# pior resultado possível numa rotina cujo valor inteiro é ser confiável.
_STATUS_QUE_MOVE_SALDO = TxStatus.CONFIRMED


@dataclass
class Achado:
    """Uma divergência. `chave` identifica o objeto; `delta` quantifica quando faz sentido."""

    tipo: str
    chave: str
    detalhe: str
    delta_pts: int | None = None

    def __str__(self) -> str:
        d = f" (Δ {self.delta_pts:+d} pts)" if self.delta_pts is not None else ""
        return f"[{self.tipo}] {self.chave}{d} — {self.detalhe}"


@dataclass
class Relatorio:
    gerado_em: datetime
    carteiras_verificadas: int = 0
    transacoes_verificadas: int = 0
    achados: list[Achado] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.achados

    @property
    def exposicao_pts(self) -> int:
        """Soma dos deltas em módulo — o tamanho do buraco, em pontos."""
        return sum(abs(a.delta_pts) for a in self.achados if a.delta_pts is not None)

    def resumo(self) -> str:
        if self.ok:
            return (
                f"OK · {self.carteiras_verificadas} carteiras e "
                f"{self.transacoes_verificadas} transações conferem"
            )
        por_tipo: dict[str, int] = {}
        for a in self.achados:
            por_tipo[a.tipo] = por_tipo.get(a.tipo, 0) + 1
        partes = ", ".join(f"{k}={v}" for k, v in sorted(por_tipo.items()))
        return (
            f"DIVERGÊNCIA · {len(self.achados)} achado(s) [{partes}] · "
            f"exposição {self.exposicao_pts} pts"
        )


# --------------------------------------------------------------------------- #
# Invariantes                                                                  #
# --------------------------------------------------------------------------- #
def _saldo_bate_com_ledger(rel: Relatorio) -> None:
    """`Wallet.balance_pts` == soma das transações CONFIRMED daquela carteira.

    É a invariante central: se ela cai, o saldo exibido ao usuário não tem
    lastro na história e qualquer extrato vira ficção.
    """
    somas = dict(
        db.session.execute(
            select(Transaction.wallet_id, func.coalesce(func.sum(Transaction.amount_pts), 0))
            .where(Transaction.status == _STATUS_QUE_MOVE_SALDO)
            .group_by(Transaction.wallet_id)
        ).all()
    )

    for wallet in db.session.execute(select(Wallet)).scalars():
        rel.carteiras_verificadas += 1
        esperado = int(somas.get(wallet.id, 0))
        if wallet.balance_pts != esperado:
            rel.achados.append(
                Achado(
                    tipo="saldo_divergente",
                    chave=f"wallet={wallet.id} user={wallet.user_id}",
                    detalhe=f"saldo={wallet.balance_pts} · ledger={esperado}",
                    delta_pts=wallet.balance_pts - esperado,
                )
            )
        # A CheckConstraint cobre inserções novas, mas dado anterior a ela (ou
        # vindo de migração) pode estar negativo. Checar é barato.
        if wallet.balance_pts < 0:
            rel.achados.append(
                Achado(
                    tipo="saldo_negativo",
                    chave=f"wallet={wallet.id}",
                    detalhe="saldo negativo — usuário deve pontos ao sistema",
                    delta_pts=wallet.balance_pts,
                )
            )


def _idempotencia_sem_duplicata(rel: Relatorio) -> None:
    """Nenhuma `idempotency_key` pode aparecer duas vezes na mesma carteira.

    Duas linhas com a mesma chave significam que a proteção contra retry falhou
    — um crédito (ou débito) aconteceu duas vezes.

    NOTA: hoje o banco já impede isso via `UniqueConstraint(wallet_id,
    idempotency_key)` (`uq_tx_idempotency`), então na prática este check é um
    **backstop**, não uma defesa de primeira linha. Vale mantê-lo por dois
    motivos: dado anterior à criação da constraint pode ter passado, e em
    Postgres `NULL` não conflita — se algum caminho de código parar de gerar a
    chave, a proteção desaparece em silêncio e só uma contagem revelaria.
    """
    dupes = db.session.execute(
        select(
            Transaction.wallet_id,
            Transaction.idempotency_key,
            func.count().label("n"),
            func.coalesce(func.sum(Transaction.amount_pts), 0).label("total"),
        )
        .where(Transaction.idempotency_key.is_not(None))
        .group_by(Transaction.wallet_id, Transaction.idempotency_key)
        .having(func.count() > 1)
    ).all()

    for wallet_id, chave, n, total in dupes:
        rel.achados.append(
            Achado(
                tipo="idempotencia_duplicada",
                chave=f"wallet={wallet_id} key={chave}",
                detalhe=f"{n} transações com a mesma chave",
                delta_pts=int(total),
            )
        )


def _cobranca_paga_tem_credito(rel: Relatorio) -> None:
    """Toda PixCharge PAID precisa de uma transação de crédito apontando para ela.

    Sem isso, o cliente pagou e não recebeu — o pior defeito possível neste
    produto, e o mais silencioso: nada falha, o dinheiro só não vira ponto.
    """
    creditos = set(
        db.session.execute(
            select(Transaction.reference)
            .where(
                Transaction.type == TxType.PURCHASE,
                Transaction.status == _STATUS_QUE_MOVE_SALDO,
                Transaction.reference.is_not(None),
            )
        ).scalars()
    )

    pagas = db.session.execute(
        select(PixCharge).where(PixCharge.status == PixChargeStatus.PAID)
    ).scalars()
    for charge in pagas:
        if charge.id not in creditos:
            rel.achados.append(
                Achado(
                    tipo="cobranca_paga_sem_credito",
                    chave=f"charge={charge.id} user={charge.user_id}",
                    detalhe=(
                        f"PAID de R$ {charge.amount_cents / 100:.2f} sem transação "
                        "de crédito — cliente pagou e não recebeu pontos"
                    ),
                )
            )


def _resgate_tem_debito(rel: Relatorio) -> None:
    """Payout que não falhou precisa do débito correspondente.

    O inverso do caso acima: pontos sairiam da carteira só na hora do pagamento,
    e um payout pago sem débito é dinheiro saindo de graça.
    """
    debitos = set(
        db.session.execute(
            select(Transaction.reference)
            .where(
                Transaction.type == TxType.REDEEM,
                Transaction.status == _STATUS_QUE_MOVE_SALDO,
                Transaction.reference.is_not(None),
            )
        ).scalars()
    )

    vivos = db.session.execute(
        select(PixPayout).where(
            PixPayout.status.in_(
                [PixPayoutStatus.PAID, PixPayoutStatus.PROCESSING, PixPayoutStatus.REQUESTED]
            )
        )
    ).scalars()
    for payout in vivos:
        if payout.id not in debitos:
            rel.achados.append(
                Achado(
                    tipo="resgate_sem_debito",
                    chave=f"payout={payout.id} user={payout.user_id}",
                    detalhe=(
                        f"status={payout.status.value} sem transação de débito — "
                        "resgate em curso sem os pontos terem saído da carteira"
                    ),
                )
            )


# --------------------------------------------------------------------------- #
# API pública                                                                  #
# --------------------------------------------------------------------------- #
def conciliar() -> Relatorio:
    """Roda todas as invariantes. Não escreve nada."""
    rel = Relatorio(gerado_em=datetime.now(timezone.utc))
    rel.transacoes_verificadas = int(
        db.session.execute(select(func.count()).select_from(Transaction)).scalar() or 0
    )
    _saldo_bate_com_ledger(rel)
    _idempotencia_sem_duplicata(rel)
    _cobranca_paga_tem_credito(rel)
    _resgate_tem_debito(rel)
    return rel
