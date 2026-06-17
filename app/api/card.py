"""Cartão Blaxx + níveis de cliente (loyalty tiers) + Apple Wallet (.pkpass).

Endpoints (prefixo /card):
  GET  /card          → estado do cartão do usuário (saldo, lifetime, nível,
                        progresso) + lista dos 4 níveis. [auth]
  GET  /card/tiers    → catálogo público dos 4 níveis (sem dados do usuário).
  GET  /card/pass     → baixa o .pkpass assinado p/ a carteira do iPhone. [auth]
                        503 se o Apple Wallet ainda não estiver configurado.
  GET  /card/pass/status → diz se a geração de pass está disponível (p/ a UI
                        decidir habilitar/ocultar o botão "Adicionar à Wallet").
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, send_file
import io

from ..config import Config
from ..services import loyalty
from ..services.passkit import PassNotConfigured, build_pkpass
from .auth import login_required

bp = Blueprint("card", __name__)


@bp.get("/")
@login_required
def get_card():
    state = loyalty.tier_state(g.current_user)
    return jsonify({
        "member": {
            "id": g.current_user.id[:8].upper(),
            "name": g.current_user.name,
        },
        "balance_pts": state["balance_pts"],
        "lifetime_points": state["lifetime_points"],
        "tier": state["tier"],
        "next_tier": state["next_tier"],
        "points_to_next": state["points_to_next"],
        "progress_pct": state["progress_pct"],
        "tiers": Config.tiers_catalog(),
        "wallet_pass_available": Config.apple_pass_configured(),
    })


@bp.get("/tiers")
def list_tiers():
    """Catálogo público dos níveis (usado em telas de marketing/onboarding)."""
    return jsonify({"tiers": Config.tiers_catalog()})


@bp.get("/pass/status")
@login_required
def pass_status():
    return jsonify({"available": Config.apple_pass_configured()})


@bp.get("/pass")
@login_required
def get_pass():
    state = loyalty.tier_state(g.current_user)
    try:
        blob = build_pkpass(g.current_user, state)
    except PassNotConfigured as exc:
        return jsonify({
            "error": "wallet_not_configured",
            "message": str(exc),
            "detail": "Apple Wallet será habilitado assim que o certificado "
                      "Pass Type ID for configurado no servidor.",
        }), 503
    except Exception as exc:  # pragma: no cover - falha de assinatura/IO
        current_app.logger.exception("Falha ao gerar .pkpass: %s", exc)
        return jsonify({"error": "pass_generation_failed"}), 500

    return send_file(
        io.BytesIO(blob),
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name="cartao-blaxx.pkpass",
    )
