"""Cria dados iniciais do Blaxx Pontos. Idempotente.

Popula:
  - 2 usuários demo (mariana / lucas)
  - 8 parceiros credenciados
  - 10 benefícios resgatáveis
  - 3 campanhas
  - Notificações de boas-vindas

Rode com: `python seed.py`
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import create_app
from app.extensions import db
from app.models import (
    TxType, User, Wallet, Partner, Benefit, Campaign, Notification,
)
from app.services import wallet as wallet_svc


SEED_USERS = [
    # Mariana é admin no seed local (acessa /admin). Em prod use UPDATE manual.
    {"name": "Mariana Costa", "email": "mariana@blaxx.com", "cpf": "12345678900",
     "password": "123456", "pix_key": "mariana@blaxx.com", "initial_pts": 84_750,
     "role": "admin", "is_vip": True},
    {"name": "Lucas Andrade", "email": "lucas@blaxx.com", "cpf": "98765432100",
     "password": "123456", "pix_key": "lucas@blaxx.com", "initial_pts": 5_000,
     "role": "user", "is_vip": False},
]

SEED_PARTNERS = [
    # Taxas reescritas no formato "1 pt a cada R$ X" para refletir 1 pt = R$ 0,09
    # mantendo cashback efetivo equivalente ao anterior:
    #   antes "5 pts a cada R$ 1" (R$0,05 cashback) -> agora "1 pt a cada R$ 1,80"
    {"name": "Pão & Cia", "category": "Mercados", "logo_emoji": "🛒",
     "accrual_rule": "1 pt a cada R$ 1,80 gasto",
     "description": "Rede de supermercados com 180 lojas no Sudeste."},
    {"name": "FarmaPlus", "category": "Farmácias", "logo_emoji": "⊕",
     "accrual_rule": "1 pt a cada R$ 3,00 em genéricos",
     "description": "Maior rede de farmácias do programa."},
    {"name": "PostoBR", "category": "Combustível", "logo_emoji": "⛽",
     "accrual_rule": "1 pt a cada 4 litros abastecidos",
     "description": "Combustível com qualidade certificada."},
    {"name": "FlixZone", "category": "Streaming", "logo_emoji": "▶",
     "accrual_rule": "10% de cashback em pontos",
     "description": "Filmes, séries e originais Blaxx."},
    {"name": "Sabor Local", "category": "Restaurantes", "logo_emoji": "🍽",
     "accrual_rule": "1 pt a cada R$ 2,25 consumido",
     "description": "Rede de bistrôs em 12 cidades."},
    {"name": "ShopVerde", "category": "E-commerce", "logo_emoji": "🛍",
     "accrual_rule": "5% de cashback em pontos",
     "description": "Marketplace de moda, casa e tecnologia."},
    {"name": "AeroFly", "category": "Viagens", "logo_emoji": "✈",
     "accrual_rule": "1 pt a cada R$ 1,15 em passagens",
     "description": "Companhia aérea parceira para resgate de milhas."},
    {"name": "EduMais", "category": "Educação", "logo_emoji": "✦",
     "accrual_rule": "1 pt a cada R$ 1,50 em cursos",
     "description": "Cursos online com desconto para clientes Blaxx."},
]

SEED_BENEFITS = [
    {"name": "Voucher Supermercado R$ 50", "partner": "Pão & Cia",
     "description": "Voucher de R$ 50 para usar em qualquer loja Pão & Cia.",
     "category": "voucher", "cost_pts": 5_000, "image_emoji": "🛒",
     "tag": "Mais resgatado", "expires_in_days": 180},
    {"name": "Combo medicamento básico", "partner": "FarmaPlus",
     "description": "Voucher de R$ 30 em medicamentos genéricos.",
     "category": "voucher", "cost_pts": 3_000, "image_emoji": "⊕",
     "tag": "Popular", "expires_in_days": 90},
    {"name": "30L de gasolina", "partner": "PostoBR",
     "description": "Crédito equivalente a 30 litros (~R$ 180).",
     "category": "voucher", "cost_pts": 18_000, "image_emoji": "⛽",
     "expires_in_days": 60},
    {"name": "1 mês FlixZone Plus", "partner": "FlixZone",
     "description": "Acesso premium por 30 dias com 4 telas simultâneas.",
     "category": "assinatura", "cost_pts": 4_500, "image_emoji": "▶",
     "tag": "Streaming", "expires_in_days": 365},
    {"name": "Jantar para 2 — Sabor Local", "partner": "Sabor Local",
     "description": "Voucher para entrada + 2 pratos principais + sobremesa.",
     "category": "experiencia", "cost_pts": 9_000, "image_emoji": "🍽",
     "tag": "Premium", "expires_in_days": 120},
    {"name": "Frete grátis ShopVerde", "partner": "ShopVerde",
     "description": "Frete grátis em qualquer compra no ShopVerde.",
     "category": "desconto", "cost_pts": 800, "image_emoji": "🛍",
     "tag": "Rápido", "expires_in_days": 30},
    {"name": "Passagem nacional ida+volta", "partner": "AeroFly",
     "description": "Voucher equivalente a uma passagem doméstica básica.",
     "category": "viagem", "cost_pts": 28_000, "image_emoji": "✈",
     "tag": "Premium", "expires_in_days": 180},
    {"name": "Curso online a sua escolha", "partner": "EduMais",
     "description": "Acesso vitalício a qualquer curso do catálogo EduMais.",
     "category": "educacao", "cost_pts": 7_500, "image_emoji": "✦",
     "expires_in_days": 365},
    {"name": "Sorteio R$ 1.000 — Blaxx", "partner": None,
     "description": "Cupom para o sorteio mensal Blaxx. R$ 1.000 em pontos extras.",
     "category": "sorteio", "cost_pts": 500, "image_emoji": "★",
     "tag": "Sorteios", "stock": 1_000, "expires_in_days": 30},
    {"name": "Doação Instituto Blaxx", "partner": None,
     "description": "Converta seus pontos em doação para o Instituto Blaxx Ed.",
     "category": "social", "cost_pts": 1_000, "image_emoji": "♡",
     "expires_in_days": 365},
]

SEED_CAMPAIGNS = [
    {"name": "Maio em dobro",
     "description": "Compre em parceiros selecionados em maio e acelere para o próximo nível.",
     "mechanic": "Gaste R$ 500 em parceiros elegíveis e ganhe 2.000 pts extras.",
     "target_brl": 50_000, "reward_pts": 2_000},
    {"name": "Família engajada",
     "description": "Convide 3 amigos e ganhe um bônus especial.",
     "mechanic": "A cada R$ 100 movimentados via P2P, contam R$ 50 para a meta.",
     "target_brl": 30_000, "reward_pts": 1_500},
    {"name": "Pacote Premium",
     "description": "Compre o pacote Black até 30/06 e ganhe um voucher exclusivo.",
     "mechanic": "Compre 1 pacote Black (R$ 2.142,00) e ganhe 5.000 pts adicionais.",
     "target_brl": 214_200, "reward_pts": 5_000},
]


def main() -> None:
    app = create_app()
    with app.app_context():
        # --- Usuários ---
        for u in SEED_USERS:
            user = db.session.query(User).filter_by(email=u["email"]).one_or_none()
            if user:
                print(f"  [skip] usuário {u['email']} já existe")
                continue
            user = User(
                name=u["name"], email=u["email"], cpf=u["cpf"],
                pix_key=u["pix_key"],
                role=u.get("role", "user"),
                is_vip=u.get("is_vip", False),
            )
            user.set_password(u["password"])
            db.session.add(user)
            db.session.flush()
            db.session.add(Wallet(user_id=user.id))
            db.session.flush()
            if u["initial_pts"]:
                wallet_svc.credit(
                    user_id=user.id, amount_pts=u["initial_pts"],
                    tx_type=TxType.BONUS, description="Saldo inicial (seed)",
                )
            db.session.add(Notification(
                user_id=user.id, type="system",
                title="Bem-vindo ao BlaXx",
                body="Sua carteira está pronta. Explore parceiros e campanhas.",
                icon="★",
            ))
            db.session.commit()
            print(f"  [ok]   usuário {u['email']} → {u['initial_pts']} pts")

        # --- Parceiros ---
        partners_by_name: dict[str, Partner] = {}
        for p in SEED_PARTNERS:
            partner = db.session.query(Partner).filter_by(name=p["name"]).one_or_none()
            if partner is None:
                partner = Partner(**p)
                db.session.add(partner)
                db.session.flush()
                print(f"  [ok]   parceiro {p['name']}")
            else:
                print(f"  [skip] parceiro {p['name']} já existe")
            partners_by_name[p["name"]] = partner
        db.session.commit()

        # --- Benefícios ---
        for b in SEED_BENEFITS:
            existing = db.session.query(Benefit).filter_by(name=b["name"]).one_or_none()
            if existing:
                print(f"  [skip] benefício {b['name']} já existe")
                continue
            partner_id = None
            if b["partner"]:
                partner_id = partners_by_name[b["partner"]].id
            db.session.add(Benefit(
                name=b["name"], description=b["description"],
                category=b["category"], cost_pts=b["cost_pts"],
                image_emoji=b["image_emoji"], tag=b.get("tag"),
                stock=b.get("stock", -1),
                expires_in_days=b["expires_in_days"],
                partner_id=partner_id,
            ))
            print(f"  [ok]   benefício {b['name']} ({b['cost_pts']} pts)")
        db.session.commit()

        # --- Campanhas ---
        end = datetime.now(timezone.utc) + timedelta(days=30)
        for c in SEED_CAMPAIGNS:
            existing = db.session.query(Campaign).filter_by(name=c["name"]).one_or_none()
            if existing:
                print(f"  [skip] campanha {c['name']} já existe")
                continue
            db.session.add(Campaign(
                name=c["name"], description=c["description"], mechanic=c["mechanic"],
                target_brl=c["target_brl"], reward_pts=c["reward_pts"],
                period_end=end,
            ))
            print(f"  [ok]   campanha {c['name']}")
        db.session.commit()

        print("\nSeed completo. Use os logins:")
        for u in SEED_USERS:
            print(f"  {u['email']} / {u['password']}")


if __name__ == "__main__":
    main()
