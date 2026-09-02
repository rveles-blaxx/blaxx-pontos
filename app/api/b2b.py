"""API das redes parceiras (B2B).

Autenticação é por CHAVE DE MÁQUINA, não por JWT de usuário: quem chama aqui é
o PDV da rede, não uma pessoa. A chave vai em `Authorization: Bearer blx_...`
ou em `X-API-Key`.

Superfície deliberadamente pequena — uma rede precisa de quatro coisas:
identificar-se, pontuar uma compra, conferir o que enviou e saber quanto deve.

Privacidade: nenhuma resposta devolve saldo, nome completo ou histórico do
cliente. A rede fica sabendo apenas quantos pontos aquela compra gerou. Ela já
conhece o CPF que digitou; não ganha nada além disso.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, g, jsonify, request

from ..extensions import db, limiter
from ..models import MerchantAccrual
from ..services import b2b as b2b_svc

bp = Blueprint("b2b", __name__)


def merchant_key_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        raw = request.headers.get("X-API-Key")
        if not raw:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                raw = auth[7:].strip()
        merchant = b2b_svc.authenticate(raw)
        if merchant is None:
            return jsonify({"error": "unauthorized", "code": "invalid_api_key"}), 401
        g.current_merchant = merchant
        return fn(*args, **kwargs)
    return wrapper


def _merchant_rate_key() -> str:
    """Rate limit POR REDE, não por IP: um PDV atrás de NAT não deve consumir
    a cota de outro, e uma rede com problema não derruba as demais."""
    m = getattr(g, "current_merchant", None)
    return f"merchant:{m.id}" if m is not None else (request.remote_addr or "anon")


def _erro(exc: b2b_svc.B2BError):
    return jsonify({"error": str(exc), "code": exc.code}), exc.status


@bp.get("/me")
@merchant_key_required
def me():
    """Identidade e contrato vigente da rede."""
    return jsonify(g.current_merchant.to_dict())


@bp.post("/accrual")
@merchant_key_required
@limiter.limit("600 per minute", key_func=_merchant_rate_key)
def accrual():
    """Registra uma compra e credita os pontos ao cliente.

    Body: {cpf | card_id, amount_cents, store_code?}
    Header opcional (recomendado): `Idempotency-Key`.

    Sem `Idempotency-Key` o PDV que repetir a chamada credita de novo — a rede
    é quem sabe o identificador do cupom fiscal, então a chave tem de vir dela.
    """
    data = request.get_json(silent=True) or {}
    idem = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    try:
        amount_cents = int(data.get("amount_cents") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "amount_cents deve ser inteiro",
                        "code": "invalid_amount"}), 400

    try:
        registro = b2b_svc.award(
            g.current_merchant,
            amount_cents=amount_cents,
            cpf=data.get("cpf"),
            card_id=data.get("card_id"),
            store_code=(data.get("store_code") or None),
            idempotency_key=idem,
        )
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return _erro(exc)

    db.session.commit()
    return jsonify(registro.to_dict()), 201


@bp.get("/accruals")
@merchant_key_required
def accruals():
    """Últimos lançamentos enviados pela rede (conferência de PDV)."""
    try:
        limit = min(int(request.args.get("limit") or 50), 200)
    except (TypeError, ValueError):
        limit = 50
    linhas = (
        db.session.query(MerchantAccrual)
        .filter_by(merchant_id=g.current_merchant.id)
        .order_by(MerchantAccrual.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [x.to_dict() for x in linhas]})


@bp.get("/statement")
@merchant_key_required
def statement():
    """Quanto a rede deve no período. Datas em ISO (`from`, `to`)."""
    def _parse(nome: str):
        valor = request.args.get(nome)
        if not valor:
            return None
        try:
            return datetime.fromisoformat(valor).replace(tzinfo=None)
        except ValueError:
            return None

    return jsonify(b2b_svc.statement(
        g.current_merchant, since=_parse("from"), until=_parse("to")
    ))


# =========================================================================== #
# Painel da rede — sessão HUMANA, separada da chave de PDV                    #
# =========================================================================== #
#
# Por que dois trilhos: a chave de máquina EMITE pontos e vive num PDV, muitas
# vezes num terminal físico compartilhado. O painel só LÊ. Se fossem a mesma
# credencial, qualquer pessoa com acesso ao painel poderia emitir pontos, e uma
# chave copiada do PDV abriria o faturamento da rede.

from flask_jwt_extended import (  # noqa: E402
    create_access_token, get_jwt, jwt_required,
)

from ..models import Merchant, MerchantApiKey, MerchantUser  # noqa: E402

PANEL_SCOPE = "merchant_panel"


def merchant_panel_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("scope") != PANEL_SCOPE:
            return jsonify({"error": "unauthorized", "code": "wrong_scope"}), 403
        u = db.session.get(MerchantUser, claims.get("mu"))
        if u is None or not u.is_active:
            return jsonify({"error": "unauthorized", "code": "inactive_user"}), 401
        if u.merchant is None or not u.merchant.is_active:
            return jsonify({"error": "rede inativa", "code": "merchant_inactive"}), 403
        g.panel_user = u
        g.current_merchant = u.merchant
        return fn(*args, **kwargs)
    return jwt_required()(wrapper)


@bp.post("/panel/login")
@limiter.limit("10 per minute")
def panel_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("password") or ""

    u = db.session.query(MerchantUser).filter_by(email=email).one_or_none()
    # Mensagem única para e-mail inexistente e senha errada: senão o painel
    # vira oráculo de quais e-mails têm conta.
    if u is None or not u.is_active or not u.check_password(senha):
        return jsonify({"error": "credenciais inválidas",
                        "code": "invalid_credentials"}), 401

    u.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()

    token = create_access_token(
        identity=u.id,
        additional_claims={"scope": PANEL_SCOPE, "mu": u.id,
                           "merchant_id": u.merchant_id},
    )
    return jsonify({"token": token, "user": u.to_dict(),
                    "merchant": u.merchant.to_dict()})


@bp.get("/panel/summary")
@merchant_panel_required
def panel_summary():
    return jsonify(b2b_svc.panel_summary(g.current_merchant))


@bp.get("/panel/accruals")
@merchant_panel_required
def panel_accruals():
    try:
        limit = min(int(request.args.get("limit") or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    linhas = (
        db.session.query(MerchantAccrual)
        .filter_by(merchant_id=g.current_merchant.id)
        .order_by(MerchantAccrual.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [x.to_dict() for x in linhas]})


@bp.get("/panel/keys")
@merchant_panel_required
def panel_keys():
    """Lista as chaves do PDV. Nunca devolve o segredo — só o prefixo, que
    serve para a rede saber QUAL chave revogar."""
    if g.panel_user.role != "owner":
        return jsonify({"error": "somente o responsável", "code": "forbidden"}), 403
    linhas = (
        db.session.query(MerchantApiKey)
        .filter_by(merchant_id=g.current_merchant.id)
        .order_by(MerchantApiKey.created_at.desc())
        .all()
    )
    return jsonify({"items": [{
        "id": k.id, "prefix": k.prefix, "label": k.label,
        "is_active": k.is_active and k.revoked_at is None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    } for k in linhas]})


@bp.post("/panel/keys")
@merchant_panel_required
def panel_create_key():
    """Gera chave nova. O segredo aparece UMA vez, nesta resposta."""
    if g.panel_user.role != "owner":
        return jsonify({"error": "somente o responsável", "code": "forbidden"}), 403
    label = (request.get_json(silent=True) or {}).get("label") or "PDV"
    _, raw = b2b_svc.issue_api_key(g.current_merchant, label=label[:80])
    db.session.commit()
    return jsonify({"key": raw, "warning": "guarde agora; não é recuperável"}), 201


@bp.delete("/panel/keys/<key_id>")
@merchant_panel_required
def panel_revoke_key(key_id: str):
    if g.panel_user.role != "owner":
        return jsonify({"error": "somente o responsável", "code": "forbidden"}), 403
    k = db.session.query(MerchantApiKey).filter_by(
        id=key_id, merchant_id=g.current_merchant.id).one_or_none()
    if k is None:
        return jsonify({"error": "chave não encontrada"}), 404
    k.is_active = False
    k.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return jsonify({"ok": True, "prefix": k.prefix})


# ─────────────────── estorno (chamado pelo PDV) ─────────────────── #

@bp.post("/accrual/<accrual_id>/reverse")
@merchant_key_required
@limiter.limit("300 per minute", key_func=_merchant_rate_key)
def reverse_accrual(accrual_id: str):
    """Desfaz um acúmulo pelo id devolvido na emissão."""
    motivo = (request.get_json(silent=True) or {}).get("reason")
    try:
        r = b2b_svc.reverse(g.current_merchant, accrual_id=accrual_id, reason=motivo)
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return _erro(exc)
    db.session.commit()
    return jsonify(_corpo_estorno(r))


@bp.post("/accrual/reverse")
@merchant_key_required
@limiter.limit("300 per minute", key_func=_merchant_rate_key)
def reverse_by_key():
    """Desfaz pelo identificador do cupom, quando o PDV não guardou o id.

    É o caminho realista: o caixa tem o número da nota, não o UUID que a API
    devolveu na venda.
    """
    d = request.get_json(silent=True) or {}
    chave = d.get("idempotency_key") or request.headers.get("Idempotency-Key")
    try:
        r = b2b_svc.reverse(g.current_merchant, idempotency_key=chave,
                            reason=d.get("reason"))
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return _erro(exc)
    db.session.commit()
    return jsonify(_corpo_estorno(r))


def _corpo_estorno(r) -> dict:
    corpo = r.to_dict()
    nao_recuperados = r.points_awarded - r.reversed_points
    corpo["points_not_recovered"] = nao_recuperados
    # O caixa precisa ver isto na hora: houve pontos que não voltaram porque o
    # cliente já os gastou, e a rede segue devendo por eles.
    corpo["note"] = (
        "Estorno completo."
        if nao_recuperados == 0 else
        f"{nao_recuperados} pts já haviam sido usados pelo cliente e não puderam "
        f"ser retirados; a cobrança dessa parte permanece."
    )
    return corpo


@bp.get("/panel/invoices")
@merchant_panel_required
def panel_invoices():
    """Faturas da rede + o que ainda está em aberto para a próxima."""
    from ..models import MerchantInvoice
    linhas = (
        db.session.query(MerchantInvoice)
        .filter_by(merchant_id=g.current_merchant.id)
        .order_by(MerchantInvoice.created_at.desc())
        .limit(36)
        .all()
    )
    return jsonify({
        "items": [f.to_dict() for f in linhas],
        "open_balance": b2b_svc.open_balance(g.current_merchant),
        "unpaid_brl": round(b2b_svc.unpaid_total_cents(g.current_merchant) / 100, 2),
    })
