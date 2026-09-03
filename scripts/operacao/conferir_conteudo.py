#!/usr/bin/env python3
"""Confere CONTEUDO, nao status. A varredura so olha codigo HTTP: 200 com lista
vazia passava como OK — e lista vazia era o proprio bug do M-4."""
import getpass, json, urllib.error, urllib.request

API = "https://blaxx-pontos-exe.onrender.com"

def get(rota, tok):
    r = urllib.request.Request(API + rota, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_erro": e.code}

email = input("e-mail: ").strip()
senha = getpass.getpass("senha: ")
req = urllib.request.Request(API + "/auth/login",
    data=json.dumps({"email": email, "password": senha}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
del senha
with urllib.request.urlopen(req, timeout=90) as r:
    tok = json.loads(r.read())["token"]

ok = True
def checa(rotulo, valor, esperado, cond):
    global ok
    bom = cond(valor)
    ok = ok and bom
    print(f"  {'OK  ' if bom else 'FALHA'} {rotulo}: {valor}   (esperado: {esperado})")

print("\nCONTEUDO")
s = get("/user/sessions", tok).get("sessions", [])
checa("sessoes listadas (M-4)", len(s), ">= 1", lambda v: v >= 1)
if s:
    tem_jti = all(x.get("id") for x in s)
    checa("toda sessao tem id revogavel", tem_jti, "True", lambda v: v is True)

checa("beneficios", len(get("/benefits/", tok).get("items", [])), "10", lambda v: v == 10)
checa("campanhas", len(get("/campaigns/", tok).get("items", [])), "3", lambda v: v == 3)
checa("redes B2B", len(get("/admin/merchants", tok).get("items", [])), "3", lambda v: v == 3)

pk = get("/pix/packages", tok)
n = len(pk) if isinstance(pk, dict) else 0
checa("pacotes (M-3)", n, "4", lambda v: v == 4)

print("\n" + ("tudo conferido" if ok else "*** algo divergiu — veja acima ***"))
