#!/usr/bin/env python3
"""Varredura do painel admin contra producao.

Por que existe: em 02/09 promovemos o primeiro admin da historia do produto e,
na primeira chamada, /admin/stats deu 500 — duas causas, ambas latentes havia
meses. As outras rotas do painel nunca foram exercitadas com dados reais.

SEGURANCA: so faz GET. Nos endpoints de escrita manda corpo VAZIO de proposito
— espera 400 (validacao). Nao cria, nao altera e nao apaga nada.
"""
import getpass, json, urllib.error, urllib.request

API = "https://blaxx-pontos-exe.onrender.com"

def req(rota, metodo="GET", corpo=None, token=None):
    data = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(API + rota, data=data, method=metodo,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, (resp.read() or b"").decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"

email = input("e-mail do admin: ").strip()
senha = getpass.getpass("senha: ")
st, body = req("/auth/login", "POST", {"email": email, "password": senha})
del senha
tok = json.loads(body).get("token") if st == 200 else None
if not tok:
    print(f"login falhou ({st})"); raise SystemExit(1)
print("login OK\n")

# um id real para as rotas parametrizadas
st, body = req("/admin/users?limit=1", token=tok)
uid = None
if st == 200:
    itens = json.loads(body).get("items") or json.loads(body).get("users") or []
    if itens:
        uid = itens[0].get("id")
print(f"usuario de amostra: {uid or '(nenhum)'}\n")

LEITURA = [
    "/admin/stats", "/admin/users?limit=5", "/admin/transactions?limit=5",
    "/admin/charges/pending", "/admin/payouts/processing", "/admin/experiments",
    "/admin/alerts", "/admin/aml/alerts", "/admin/packages",
    "/admin/merchants", "/admin/invoices", "/admin/export/transactions.csv",
]
if uid:
    LEITURA.append(f"/admin/users/{uid}")

# Superficies alteradas em 02/09. Todas GET — /benefits/<id>/redeem NAO entra
# aqui: gasta pontos de verdade.
LEITURA += [
    "/user/sessions",      # M-4: deve listar a sessao deste login, nao []
    "/benefits/",          # catalogo populado hoje
    "/vouchers/",
    "/pix/packages",       # M-3: fonte unica de preco
    "/campaigns/",
]

# escrita: corpo vazio, espera 400. NAO altera nada.
VALIDACAO = [
    ("POST",  "/admin/partners"), ("POST", "/admin/benefits"),
    ("POST",  "/admin/campaigns"), ("POST", "/admin/merchants"),
    # NUNCA sondar /users/<id>/vip: corpo vazio nao e erro ali, e TOGGLE.
    # Em 02/09 esta sonda inverteu o is_vip de um usuario real em producao.
    # VIP nao tem teto diario de resgate, entao o efeito e financeiro.
    # Regra: so sondar endpoint cujo corpo vazio seja comprovadamente invalido.
    ("PATCH", f"/admin/users/{uid}/role" if uid else None),
]

quebrados = []
print("LEITURA")
for rota in LEITURA:
    st, body = req(rota, token=tok)
    marca = "OK  " if st == 200 else ("500 " if st == 500 else f"{st} ")
    print(f"  {marca} {rota}")
    if st >= 500 or st == 0:
        quebrados.append((rota, st, body[:200]))

print("\nVALIDACAO (corpo vazio; 400 e o esperado)")
for metodo, rota in VALIDACAO:
    if not rota:
        continue
    st, body = req(rota, metodo, {}, tok)
    marca = "OK  " if st in (400, 409, 422) else ("500 " if st == 500 else f"{st} ")
    print(f"  {marca} {metodo} {rota}")
    if st >= 500 or st == 0:
        quebrados.append((f"{metodo} {rota}", st, body[:200]))

print()
if quebrados:
    print(f"*** {len(quebrados)} ENDPOINT(S) QUEBRADO(S) ***")
    for rota, st, body in quebrados:
        print(f"  {rota} -> {st}")
else:
    print("nenhum endpoint quebrado")
