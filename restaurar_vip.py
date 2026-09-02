#!/usr/bin/env python3
"""Mostra e restaura o is_vip de um usuario.

Necessario porque a varredura de 02/09 mandou corpo vazio para
PATCH /admin/users/<id>/vip, que faz TOGGLE nesse caso — invertendo o valor
de um usuario real. Este script le o estado atual e, com confirmacao explicita,
grava o valor OPOSTO (que era o original, ja que houve exatamente uma inversao).
"""
import getpass, json, urllib.error, urllib.request

API = "https://blaxx-pontos-exe.onrender.com"
UID = "3045ecc458b346f1a2bc649e0b688c3b"

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

email = input("e-mail do admin: ").strip()
senha = getpass.getpass("senha: ")
st, body = req("/auth/login", "POST", {"email": email, "password": senha})
del senha
tok = json.loads(body).get("token") if st == 200 else None
if not tok:
    print(f"login falhou ({st})"); raise SystemExit(1)

st, body = req(f"/admin/users/{UID}", token=tok)
if st != 200:
    print(f"nao consegui ler o usuario ({st})"); raise SystemExit(1)
d = json.loads(body)
u = d.get("user", d)
atual = u.get("is_vip")
print(f"\nusuario: {u.get('name')} <{u.get('email')}>")
print(f"is_vip AGORA:      {atual}")
print(f"is_vip ANTES era:  {not atual}   (houve exatamente uma inversao)")

if input("\nrestaurar para o valor original? (s/N) ").strip().lower() != "s":
    print("nada alterado."); raise SystemExit(0)

st, body = req(f"/admin/users/{UID}/vip", "PATCH", {"is_vip": (not atual)}, tok)
print(f"resultado ({st}): {body}")
