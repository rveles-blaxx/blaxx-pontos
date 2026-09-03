#!/usr/bin/env python3
"""Testa cada endpoint admin que os scripts de carga usam.

/admin/stats pode estar quebrado sem impedir o cadastro — sao rotas diferentes.
Este script diz exatamente quais funcionam.
"""
import getpass, json, urllib.error, urllib.request

API = "https://blaxx-pontos-exe.onrender.com"

def req(rota, metodo="GET", corpo=None, token=None):
    data = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(API + rota, data=data, method=metodo,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, (resp.read() or b"").decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)

email = input("e-mail: ").strip()
senha = getpass.getpass("senha: ")
st, body = req("/auth/login", "POST", {"email": email, "password": senha})
del senha
tok = json.loads(body).get("token") if st == 200 else None
if not tok:
    print(f"login falhou ({st})"); raise SystemExit(1)
print("login OK\n")

# GETs inofensivos + um POST invalido de proposito (nao cria nada; so prova
# que a rota responde 400 de validacao em vez de 500).
provas = [
    ("GET",  "/admin/stats",     None),
    ("GET",  "/admin/merchants", None),
    ("GET",  "/admin/users?limit=1", None),
    ("GET",  "/admin/invoices",  None),
    ("POST", "/admin/partners",  {}),
    ("POST", "/admin/benefits",  {}),
    ("POST", "/admin/campaigns", {}),
]
for metodo, rota, corpo in provas:
    st, body = req(rota, metodo, corpo, tok)
    veredito = {200: "OK", 201: "OK", 400: "OK (validacao)", 403: "sem permissao",
                401: "sem auth", 404: "NAO EXISTE", 500: "QUEBRADO"}.get(st, str(st))
    print(f"  {metodo:5} {rota:26} {st}  {veredito}")
    if st == 500:
        print(f"        corpo: {body[:140]}")

print("\nSe /admin/partners, /benefits e /campaigns derem 400, os scripts de")
print("carga funcionam — 400 e a validacao recusando corpo vazio, nao erro.")
