#!/usr/bin/env python3
"""Popula o catálogo em produção pelos endpoints admin.

Mesmo conteúdo do `seed_catalog.py`, mas por HTTP — o serviço roda no plano free
do Render, que não tem Shell, e o DATABASE_URL só existe nas env vars de lá.

    python3 cadastrar_catalogo.py

Idempotente: nome repetido volta 409 e o script segue. A senha é lida sem eco.

⚠️ Os 8 parceiros são FICTÍCIOS de propósito. Produção já tem 258 empresas REAIS
listadas como parceiras sem contrato (item G05); pendurar voucher resgatável em
qualquer uma delas viraria promessa comercial em nome de terceiro.
"""
import getpass
import json
import sys
import urllib.error
import urllib.request

API = "https://blaxx-pontos-exe.onrender.com"

PARCEIROS = [
    ("Pão & Cia", "Mercados", "🛒", "1 pt a cada R$ 1,80 gasto"),
    ("FarmaPlus", "Farmácias", "⊕", "1 pt a cada R$ 3,00 em genéricos"),
    ("PostoBR", "Combustível", "⛽", "1 pt a cada 4 litros abastecidos"),
    ("FlixZone", "Streaming", "▶", "10% de cashback em pontos"),
    ("Sabor Local", "Restaurantes", "🍽", "1 pt a cada R$ 2,25 consumido"),
    ("ShopVerde", "E-commerce", "🛍", "5% de cashback em pontos"),
    ("AeroFly", "Viagens", "✈", "1 pt a cada R$ 1,15 em passagens"),
    ("EduMais", "Educação", "✦", "1 pt a cada R$ 1,50 em cursos"),
]

# Preços na base única definida em 12/08 (ver seed_catalog.py).
BENEFICIOS = [
    ("Voucher Supermercado R$ 50", "Pão & Cia", "voucher", 588, "🛒", "Mais resgatado", 180, -1),
    ("Combo medicamento básico", "FarmaPlus", "voucher", 353, "⊕", "Popular", 90, -1),
    ("30L de gasolina", "PostoBR", "voucher", 2118, "⛽", None, 60, -1),
    ("1 mês FlixZone Plus", "FlixZone", "assinatura", 529, "▶", "Streaming", 365, -1),
    ("Jantar para 2 — Sabor Local", "Sabor Local", "experiencia", 1059, "🍽", "Premium", 120, -1),
    ("Frete grátis ShopVerde", "ShopVerde", "desconto", 94, "🛍", "Rápido", 30, -1),
    ("Passagem nacional ida+volta", "AeroFly", "viagem", 3294, "✈", "Premium", 180, -1),
    ("Curso online a sua escolha", "EduMais", "educacao", 882, "✦", None, 365, -1),
    ("Sorteio R$ 1.000 — BlaXx", None, "sorteio", 59, "★", "Sorteios", 30, 1000),
    ("Doação Instituto BlaXx", None, "social", 118, "♡", None, 365, -1),
]

CAMPANHAS = [
    ("Maio em dobro", "Compre em parceiros selecionados e acelere para o próximo nível.",
     "Gaste R$ 500 em parceiros elegíveis e ganhe 2.000 pts extras.", 50_000, 2_000),
    ("Família engajada", "Convide 3 amigos e ganhe um bônus especial.",
     "A cada R$ 100 movimentados via P2P, contam R$ 50 para a meta.", 30_000, 1_500),
    ("Pacote Premium", "Compre o pacote Black e ganhe um voucher exclusivo.",
     "Compre 1 pacote Black (R$ 2.142,00) e ganhe 5.000 pts adicionais.", 214_200, 5_000),
]


def chamar(rota, corpo, token=None, tentativas=3):
    """Uma falha de rede NAO pode matar o script no meio da carga.

    A primeira versao so capturava HTTPError: um timeout levantava URLError,
    a traceback subia e o restante do catalogo nunca era tentado — com a saida
    parecendo sucesso ate a linha anterior. Foi o que aconteceu em 02/09:
    8 parceiros e 2 beneficios entraram, o resto nem foi pedido.
    """
    import time
    ultimo = ""
    for n in range(tentativas):
        req = urllib.request.Request(
            API + rota, data=json.dumps(corpo).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:                     # timeout, conexao cortada, DNS
            ultimo = f"{type(e).__name__}: {e}"
            if n < tentativas - 1:
                time.sleep(2 * (n + 1))
    return 0, {"error": f"rede falhou apos {tentativas} tentativas — {ultimo}"}


def main() -> int:
    email = input("e-mail do admin: ").strip()
    senha = getpass.getpass("senha: ")
    st, body = chamar("/auth/login", {"email": email, "password": senha})
    del senha
    token = body.get("token")
    if not token:
        print(f"login falhou ({st})")
        return 1
    print("login ok\n")

    contas = {"criados": 0, "existiam": 0, "erros": 0}

    def registrar(rotulo, st, body):
        if st == 201:
            contas["criados"] += 1
            print(f"  novo   {rotulo}")
        elif st == 409:
            contas["existiam"] += 1
        else:
            contas["erros"] += 1
            print(f"  ERRO   {rotulo} → {st} {body.get('error', '')}")

    print("Parceiros")
    for nome, cat, emoji, regra in PARCEIROS:
        st, b = chamar("/admin/partners", {
            "name": nome, "category": cat, "logo_emoji": emoji, "accrual_rule": regra,
            "description": f"{cat} fictícia usada nos exemplos do catálogo."}, token)
        registrar(nome, st, b)

    print("Benefícios")
    for nome, parc, cat, custo, emoji, tag, dias, estoque in BENEFICIOS:
        corpo = {"name": nome, "category": cat, "cost_pts": custo,
                 "image_emoji": emoji, "expires_in_days": dias, "stock": estoque}
        if parc:
            corpo["partner_name"] = parc
        if tag:
            corpo["tag"] = tag
        st, b = chamar("/admin/benefits", corpo, token)
        registrar(f"{nome} ({custo} pts)", st, b)

    print("Campanhas")
    for nome, desc, mec, alvo, premio in CAMPANHAS:
        st, b = chamar("/admin/campaigns", {
            "name": nome, "description": desc, "mechanic": mec,
            "target_brl": alvo, "reward_pts": premio}, token)
        registrar(f"{nome} (+{premio} pts)", st, b)

    esperado = len(PARCEIROS) + len(BENEFICIOS) + len(CAMPANHAS)
    ok = contas["criados"] + contas["existiam"]
    print(f"\ncriados: {contas['criados']} · já existiam: {contas['existiam']} "
          f"· erros: {contas['erros']}")
    if ok < esperado or contas["erros"]:
        print(f"\n*** INCOMPLETO: {ok}/{esperado} itens no catálogo. ***")
        print("*** Rode de novo — o script é idempotente e preenche só o que falta. ***")
        return 1
    print(f"catálogo completo: {ok}/{esperado}")
    print(f"conferir: curl -s {API}/campaigns/ | head -c 200")
    return 0


if __name__ == "__main__":
    sys.exit(main())
