#!/usr/bin/env python3
"""Diz por que o login no /admin falhou, sem expor a senha.

Distingue tres casos que parecem iguais na tela:
  401 no login  -> e-mail ou senha errados (a API nao diz qual, de proposito)
  200 mas role != admin -> a conta existe, mas nao tem o papel
  200 e admin   -> credencial certa; o problema esta em outro lugar
"""
import getpass, json, sys, urllib.error, urllib.request

API = "https://blaxx-pontos-exe.onrender.com"

def post(rota, corpo, token=None):
    req = urllib.request.Request(API + rota, data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}

def get(rota, token):
    req = urllib.request.Request(API + rota, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

email = input("e-mail: ").strip()
senha = getpass.getpass("senha: ")
st, body = post("/auth/login", {"email": email, "password": senha})
del senha

if st != 200 or not body.get("token"):
    print(f"\n[1] LOGIN FALHOU ({st}) — e-mail nao cadastrado ou senha errada.")
    print("    A API nao distingue os dois de proposito (anti-enumeracao).")
    print("    Saida: POST /auth/forgot-password para este e-mail e redefinir.")
    sys.exit(1)

tok = body["token"]
st_me, me = get("/auth/me", tok)
papel = me.get("role") or "user"
print(f"\n[1] login OK · usuario: {me.get('name')} · role: {papel}")

st_adm, _ = get("/admin/stats", tok)
if st_adm == 200:
    print("[2] /admin OK — esta conta E admin. O erro estava na digitacao.")
elif st_adm == 403:
    print("[2] /admin -> 403: a conta existe mas NAO tem role=admin.")
    print(f"    id do usuario: {me.get('id')}")
    print("    Sem outro admin vivo, promover exige SQL no console do Neon:")
    print(f"      UPDATE users SET role='admin' WHERE id='{me.get('id')}';")
else:
    print(f"[2] /admin -> {st_adm} (inesperado)")
