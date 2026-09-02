"""Cadastra APENAS campanhas e benefícios de exemplo. Idempotente.

Por que existe, se já há `seed.py`
----------------------------------
O `seed.py` também cria **dois usuários com senha `123456`**, credita saldo e
manda notificação. Isso é aceitável numa base local; num banco de produção —
mesmo em homologação — é conta com senha trivial e saldo resgatável. Este script
faz o recorte seguro: só catálogo, nenhum usuário, nenhuma carteira, nenhum
crédito de pontos.

O que cria
----------
  - 8 parceiros FICTÍCIOS (Pão & Cia, FarmaPlus, …), só se ainda não existirem
  - 10 benefícios resgatáveis, ligados a esses parceiros fictícios
  - 3 campanhas ativas

Por que parceiros fictícios, e não os que já estão no banco
----------------------------------------------------------
Produção tem 258 parceiros cujas descrições dizem "Parceiro Livelo" — são
empresas REAIS (ACER, AliExpress, Allianz, Alura…). Pendurar um voucher
resgatável em qualquer uma delas cria uma promessa comercial em nome de terceiro
sem contrato. Os benefícios de exemplo ficam nos parceiros fictícios do próprio
repositório, que ninguém confunde com parceria firmada.

Preço em pontos
---------------
Cada `cost_pts` deriva do valor do próprio item — o mesmo valor que aparece na
descrição ("Voucher de R$ 50", "~R$ 180"). Os números vieram de uma decisão de
produto de 12/08 e substituem os do `seed.py`, que precificavam o catálogo numa
base diferente da usada no resgate em dinheiro. Ao acrescentar item novo, derive
o preço do valor do item pela mesma conta usada aqui, senão o catálogo volta a
ter duas bases.

Como rodar (no shell do serviço no Render, onde DATABASE_URL já existe):

    python seed_catalog.py

Rodar duas vezes não duplica nada: tudo é procurado por nome antes de criar.
Nada é apagado nem atualizado — itens já existentes são apenas pulados.
"""

from __future__ import annotations

import sys

from app import create_app
from app.extensions import db
from app.models import Benefit, Campaign, Partner


# Parceiros fictícios — nomes inventados de propósito (ver docstring).
SEED_PARTNERS = [
    {"name": "Pão & Cia", "category": "Mercados", "logo_emoji": "🛒",
     "accrual_rule": "1 pt a cada R$ 1,80 gasto",
     "description": "Rede de supermercados fictícia usada nos exemplos do catálogo."},
    {"name": "FarmaPlus", "category": "Farmácias", "logo_emoji": "⊕",
     "accrual_rule": "1 pt a cada R$ 3,00 em genéricos",
     "description": "Rede de farmácias fictícia usada nos exemplos do catálogo."},
    {"name": "PostoBR", "category": "Combustível", "logo_emoji": "⛽",
     "accrual_rule": "1 pt a cada 4 litros abastecidos",
     "description": "Rede de postos fictícia usada nos exemplos do catálogo."},
    {"name": "FlixZone", "category": "Streaming", "logo_emoji": "▶",
     "accrual_rule": "10% de cashback em pontos",
     "description": "Serviço de streaming fictício usado nos exemplos do catálogo."},
    {"name": "Sabor Local", "category": "Restaurantes", "logo_emoji": "🍽",
     "accrual_rule": "1 pt a cada R$ 2,25 consumido",
     "description": "Rede de restaurantes fictícia usada nos exemplos do catálogo."},
    {"name": "ShopVerde", "category": "E-commerce", "logo_emoji": "🛍",
     "accrual_rule": "5% de cashback em pontos",
     "description": "Marketplace fictício usado nos exemplos do catálogo."},
    {"name": "AeroFly", "category": "Viagens", "logo_emoji": "✈",
     "accrual_rule": "1 pt a cada R$ 1,15 em passagens",
     "description": "Companhia aérea fictícia usada nos exemplos do catálogo."},
    {"name": "EduMais", "category": "Educação", "logo_emoji": "✦",
     "accrual_rule": "1 pt a cada R$ 1,50 em cursos",
     "description": "Escola online fictícia usada nos exemplos do catálogo."},
]

SEED_BENEFITS = [
    {"name": "Voucher Supermercado R$ 50", "partner": "Pão & Cia",
     "description": "Voucher de R$ 50 para usar em qualquer loja Pão & Cia.",
     "category": "voucher", "cost_pts": 588, "image_emoji": "🛒",
     "tag": "Mais resgatado", "expires_in_days": 180},
    {"name": "Combo medicamento básico", "partner": "FarmaPlus",
     "description": "Voucher de R$ 30 em medicamentos genéricos.",
     "category": "voucher", "cost_pts": 353, "image_emoji": "⊕",
     "tag": "Popular", "expires_in_days": 90},
    {"name": "30L de gasolina", "partner": "PostoBR",
     "description": "Crédito equivalente a 30 litros (~R$ 180).",
     "category": "voucher", "cost_pts": 2_118, "image_emoji": "⛽",
     "expires_in_days": 60},
    {"name": "1 mês FlixZone Plus", "partner": "FlixZone",
     "description": "Acesso premium por 30 dias com 4 telas simultâneas.",
     "category": "assinatura", "cost_pts": 529, "image_emoji": "▶",
     "tag": "Streaming", "expires_in_days": 365},
    {"name": "Jantar para 2 — Sabor Local", "partner": "Sabor Local",
     "description": "Voucher para entrada + 2 pratos principais + sobremesa.",
     "category": "experiencia", "cost_pts": 1_059, "image_emoji": "🍽",
     "tag": "Premium", "expires_in_days": 120},
    {"name": "Frete grátis ShopVerde", "partner": "ShopVerde",
     "description": "Frete grátis em qualquer compra no ShopVerde.",
     "category": "desconto", "cost_pts": 94, "image_emoji": "🛍",
     "tag": "Rápido", "expires_in_days": 30},
    {"name": "Passagem nacional ida+volta", "partner": "AeroFly",
     "description": "Voucher equivalente a uma passagem doméstica básica.",
     "category": "viagem", "cost_pts": 3_294, "image_emoji": "✈",
     "tag": "Premium", "expires_in_days": 180},
    {"name": "Curso online a sua escolha", "partner": "EduMais",
     "description": "Acesso vitalício a qualquer curso do catálogo EduMais.",
     "category": "educacao", "cost_pts": 882, "image_emoji": "✦",
     "expires_in_days": 365},
    {"name": "Sorteio R$ 1.000 — BlaXx", "partner": None,
     "description": "Cupom para o sorteio mensal BlaXx. R$ 1.000 em pontos extras.",
     "category": "sorteio", "cost_pts": 59, "image_emoji": "★",
     "tag": "Sorteios", "stock": 1_000, "expires_in_days": 30},
    {"name": "Doação Instituto BlaXx", "partner": None,
     "description": "Converta seus pontos em doação para o Instituto BlaXx Ed.",
     "category": "social", "cost_pts": 118, "image_emoji": "♡",
     "expires_in_days": 365},
]

# `target_brl` é em CENTAVOS (o `to_dict` divide por 100). 50_000 = R$ 500,00.
#
# `period_end` fica NULL de propósito: `GET /campaigns/` filtra só por
# `is_active`, sem olhar data. Gravar uma data de fim que nada respeita poria
# um prazo falso na tela. Para encerrar uma campanha, vire `is_active`.
SEED_CAMPAIGNS = [
    {"name": "Maio em dobro",
     "description": "Compre em parceiros selecionados e acelere para o próximo nível.",
     "mechanic": "Gaste R$ 500 em parceiros elegíveis e ganhe 2.000 pts extras.",
     "target_brl": 50_000, "reward_pts": 2_000},
    {"name": "Família engajada",
     "description": "Convide 3 amigos e ganhe um bônus especial.",
     "mechanic": "A cada R$ 100 movimentados via P2P, contam R$ 50 para a meta.",
     "target_brl": 30_000, "reward_pts": 1_500},
    {"name": "Pacote Premium",
     "description": "Compre o pacote Black e ganhe um voucher exclusivo.",
     "mechanic": "Compre 1 pacote Black (R$ 2.142,00) e ganhe 5.000 pts adicionais.",
     "target_brl": 214_200, "reward_pts": 5_000},
]


def main() -> int:
    app = create_app()
    with app.app_context():
        criados = {"parceiros": 0, "beneficios": 0, "campanhas": 0}
        pulados = {"parceiros": 0, "beneficios": 0, "campanhas": 0}

        # --- Parceiros fictícios (dependência dos benefícios) ---
        parceiros: dict[str, Partner] = {}
        for p in SEED_PARTNERS:
            existente = db.session.query(Partner).filter_by(name=p["name"]).one_or_none()
            if existente is not None:
                parceiros[p["name"]] = existente
                pulados["parceiros"] += 1
                continue
            novo = Partner(**p)
            db.session.add(novo)
            db.session.flush()
            parceiros[p["name"]] = novo
            criados["parceiros"] += 1
            print(f"  [novo]  parceiro  {p['name']}")

        # --- Benefícios ---
        for b in SEED_BENEFITS:
            if db.session.query(Benefit).filter_by(name=b["name"]).one_or_none() is not None:
                pulados["beneficios"] += 1
                continue
            dados = {k: v for k, v in b.items() if k != "partner"}
            nome_parceiro = b.get("partner")
            db.session.add(Benefit(
                partner_id=parceiros[nome_parceiro].id if nome_parceiro else None,
                **dados,
            ))
            criados["beneficios"] += 1
            print(f"  [novo]  benefício {b['name']} — {b['cost_pts']} pts")

        # --- Campanhas ---
        for c in SEED_CAMPAIGNS:
            if db.session.query(Campaign).filter_by(name=c["name"]).one_or_none() is not None:
                pulados["campanhas"] += 1
                continue
            db.session.add(Campaign(**c))
            criados["campanhas"] += 1
            print(f"  [nova]  campanha  {c['name']} — +{c['reward_pts']} pts")

        db.session.commit()

        print()
        print("Criados:", ", ".join(f"{v} {k}" for k, v in criados.items()))
        print("Já existiam (pulados):", ", ".join(f"{v} {k}" for k, v in pulados.items()))
        print()
        print("Confira com:")
        print("  curl -s https://blaxx-pontos-exe.onrender.com/campaigns/")
        print("  curl -s https://blaxx-pontos-exe.onrender.com/benefits/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
