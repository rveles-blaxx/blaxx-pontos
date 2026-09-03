"""Cadastra as três redes B2B e emite uma chave de API para cada. Idempotente.

    python seed_merchants.py

As chaves em claro aparecem UMA vez, na saída deste comando. O banco guarda só
o hash — rodar de novo não reimprime, apenas informa que a rede já existe. Para
uma chave nova numa rede existente:

    python seed_merchants.py --nova-chave posto

Contratos
---------
Cada rede tem dois números, e eles são coisas diferentes:

  `accrual_cents_per_point`  quanto o CLIENTE gasta para ganhar 1 ponto
  `bill_cents_per_point`     quanto a REDE paga à BlaXx por ponto emitido

O segundo precisa ser >= `Config.CENTS_PER_POINT` (custo de resgate do ponto),
senão a emissão é recusada em runtime. Com resgate a R$ 0,09 e cobrança a
R$ 0,10, a BlaXx tem ~11% de margem bruta por ponto, antes de breakage.

Resultado por vertical, com os números abaixo:

  | rede         | cliente ganha      | cashback efetivo | custo da rede |
  |--------------|--------------------|------------------|---------------|
  | Posto        | 1 pt a cada R$ 10  | 0,90%            | 1,00%         |
  | Supermercado | 1 pt a cada R$ 5   | 1,80%            | 2,00%         |
  | Farmácia     | 1 pt a cada R$ 3   | 3,00%            | 3,33%         |

⚠️ As três redes são FICTÍCIAS, com CNPJ inválido de propósito. Não repetir o
que já está no `partners`, onde 258 empresas reais aparecem como parceiras sem
contrato nenhum.
"""

from __future__ import annotations

import sys

from app import create_app
from app.extensions import db
from app.models import Merchant, MerchantVertical
from app.services import b2b as b2b_svc

REDES = {
    "posto": {
        "name": "Posto Girassol",
        "legal_name": "Girassol Distribuidora de Combustíveis Ltda (fictícia)",
        "cnpj": "11111111000191",
        "vertical": MerchantVertical.POSTO,
        "accrual_cents_per_point": 1_000,   # 1 pt a cada R$ 10,00
        "bill_cents_per_point": 10,         # R$ 0,10 por ponto
        "max_points_per_tx": 500,           # ~R$ 5.000 de abastecimento
    },
    "supermercado": {
        "name": "Supermercado Pilar",
        "legal_name": "Pilar Comércio de Alimentos S.A. (fictícia)",
        "cnpj": "22222222000172",
        "vertical": MerchantVertical.SUPERMERCADO,
        "accrual_cents_per_point": 500,     # 1 pt a cada R$ 5,00
        "bill_cents_per_point": 10,
        "max_points_per_tx": 1_000,         # ~R$ 5.000 de compra
    },
    "farmacia": {
        "name": "Farmácia Aurora",
        "legal_name": "Aurora Drogarias Ltda (fictícia)",
        "cnpj": "33333333000153",
        "vertical": MerchantVertical.FARMACIA,
        "accrual_cents_per_point": 300,     # 1 pt a cada R$ 3,00
        "bill_cents_per_point": 10,
        "max_points_per_tx": 800,           # ~R$ 2.400 de compra
    },
}


def main(argv: list[str]) -> int:
    apenas_chave = None
    if "--nova-chave" in argv:
        i = argv.index("--nova-chave")
        if i + 1 >= len(argv):
            print("uso: --nova-chave <posto|supermercado|farmacia>")
            return 2
        apenas_chave = argv[i + 1]
        if apenas_chave not in REDES:
            print(f"rede desconhecida: {apenas_chave}")
            return 2

    app = create_app()
    with app.app_context():
        for slug, dados in REDES.items():
            if apenas_chave and slug != apenas_chave:
                continue

            rede = db.session.query(Merchant).filter_by(cnpj=dados["cnpj"]).one_or_none()
            nova = rede is None
            if nova:
                rede = Merchant(**dados)
                db.session.add(rede)
                db.session.flush()

            if nova or apenas_chave:
                _, chave = b2b_svc.issue_api_key(rede, label="PDV principal")
                db.session.commit()
                print(f"\n{rede.name}  [{rede.vertical.value}]")
                print(f"  acúmulo   {rede.accrual_label()}")
                print(f"  chave     {chave}")
                print("            ^ guarde agora; não é possível recuperar depois")
            else:
                print(f"\n{rede.name}  [já existe, chave não reemitida]")

        db.session.commit()

    print("\nTeste com:")
    print("  curl -H 'X-API-Key: <chave>' https://blaxx-pontos-exe.onrender.com/b2b/me")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
