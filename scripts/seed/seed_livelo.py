"""Importa 258 parceiros Livelo no banco. Idempotente.

Uso: python3 seed_livelo.py

Lê data/livelo_partners.json (gerado a partir de Blaxx_vs_Livelo_Parceiros.xlsx).
Para cada parceiro, INSERT se não existir (match por name+category).
"""

from __future__ import annotations

import json
import os
import sys

from app import create_app
from app.extensions import db
from app.models import Partner


# Emoji por categoria — UX visual sem precisar logo PNG.
CATEGORY_EMOJI = {
    "Moda": "👗",
    "Viagens": "✈️",
    "Beleza": "💄",
    "Seguros": "🛡️",
    "Esportes": "⚽",
    "Eletrônicos": "📱",
    "Consórcio": "🏦",
    "Supermercado": "🛒",
    "Casa & Decoração": "🏠",
    "Alimentação": "🍴",
    "Saúde": "⚕️",
    "Educação": "🎓",
    "Pet Shop": "🐾",
    "Combustível": "⛽",
    "Farmácias": "💊",
    "Streaming": "📺",
    "E-commerce": "📦",
    "Restaurantes": "🍽️",
    "Cartões": "💳",
    "Bancos": "🏦",
    "Telecom": "📞",
    "Outros": "🎯",
}


def main() -> int:
    app = create_app()
    json_path = os.path.join(os.path.dirname(__file__), "data", "livelo_partners.json")
    if not os.path.exists(json_path):
        print(f"❌ arquivo não encontrado: {json_path}")
        return 1

    with open(json_path, "r", encoding="utf-8") as f:
        partners_data = json.load(f)

    with app.app_context():
        created = skipped = 0
        for p in partners_data:
            name = (p.get("name") or "").strip()
            category = (p.get("category") or "Outros").strip()
            if not name:
                continue
            existing = db.session.query(Partner).filter_by(name=name).first()
            if existing:
                skipped += 1
                continue
            emoji = CATEGORY_EMOJI.get(category, "🎯")
            partner = Partner(
                name=name,
                category=category,
                logo_emoji=emoji,
                accrual_rule=(p.get("accrual_rule") or "Pontos por R$").strip(),
                description=(p.get("description") or "")[:300],
                is_active=True,
            )
            db.session.add(partner)
            created += 1
        db.session.commit()
        print(f"\n✓ {created} parceiros criados, {skipped} já existiam (skip)")
        print(f"  Total no banco agora: {db.session.query(Partner).count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
