#!/usr/bin/env python3
"""Blaxx Pontos — Loop de testes (regressão) executável.

Roda TODAS as fases automatizáveis do checklist contra um backend em
homologação isolado (SQLite temporário, em memória), sem tocar produção.
Use antes de qualquer publicação:

    python3 qa_loop.py            # roda tudo, imprime relatório datado
    echo $?                       # 0 = tudo PASS, 1 = houve falha

Cobre: cadastro, login, recuperação, carteira, compra PIX (+expirado/dup),
envio P2P (+regras), resgate, admin (estorno/bloqueio/export), segurança.
NÃO cobre (manual/device): multi-browser, mobile, Lighthouse, 4G,
webhook real do Mercado Pago, entrega real de e-mail.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

# ─── ambiente isolado ANTES de importar o app ───
_DB = os.path.join(tempfile.gettempdir(), "blaxx_qa_loop.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["FLASK_ENV"] = "development"
os.environ.pop("SENTRY_DSN", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app                       # noqa: E402
from app.config import TestConfig                # noqa: E402
from app.extensions import db                    # noqa: E402
from app.models import User, Wallet, Benefit, PixCharge  # noqa: E402

PWD = "Blaxx@123"
STRONG = "Homolog#2026Ax"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, got: str = "") -> None:
    _results.append((name, bool(ok), got))


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def valid_cpf(seed: int) -> str:
    base = [int(d) for d in f"{seed:09d}"]

    def dv(nums):
        w = len(nums) + 1
        s = sum(n * (w - i) for i, n in enumerate(nums))
        r = (s * 10) % 11
        return 0 if r == 10 else r
    d1 = dv(base)
    d2 = dv(base + [d1])
    return "".join(map(str, base + [d1, d2]))


def main() -> int:
    app = create_app(TestConfig)

    # ─── seed ───
    with app.app_context():
        def mk(name, email, cpf, bal, role="user", verified=True):
            u = User(name=name, email=email, cpf=cpf, role=role,
                     email_verified_at=datetime.now(timezone.utc) if verified else None)
            u.set_password(PWD)
            db.session.add(u)
            db.session.flush()
            db.session.add(Wallet(user_id=u.id, balance_pts=bal, pending_pts=0))
            return u
        mk("Admin Root", "admin@blaxx.test", valid_cpf(900000001), 0, role="admin")
        mk("Teste Alpha", "alpha@blaxx.test", valid_cpf(900000002), 50000)
        mk("Teste Beta", "beta@blaxx.test", valid_cpf(900000003), 0)
        mk("Teste Zeta", "zeta@blaxx.test", valid_cpf(900000004), 1000)
        db.session.add(Benefit(name="Voucher iFood R$ 50", category="voucher",
                               cost_pts=5000, is_active=True, image_emoji="🍔"))
        db.session.commit()

    c = app.test_client()

    def login(email, pw=PWD):
        r = c.post("/auth/login", json={"email": email, "password": pw})
        return r.status_code, (r.get_json() or {}).get("token")

    def H(tok):
        return {"Authorization": f"Bearer {tok}"}

    def bal(tok):
        return c.get("/wallet/", headers=H(tok)).get_json()["balance_pts"]

    ta = login("alpha@blaxx.test")[1]
    tadm = login("admin@blaxx.test")[1]

    # ─── 1. CADASTRO ───
    section("Cadastro")
    reg = {"name": "Novo Cliente", "email": "novo@blaxx.test", "cpf": valid_cpf(123456789),
           "password": STRONG, "accept_terms": True, "accept_privacy": True, "accept_lgpd": True}
    r = c.post("/auth/register", json=reg)
    check("cadastro válido → 201 + token", r.status_code == 201 and bool((r.get_json() or {}).get("token")), f"http={r.status_code}")
    check("e-mail/CPF duplicado → 409", c.post("/auth/register", json=reg).status_code == 409)
    check("CPF inválido → 400", c.post("/auth/register", json=dict(reg, email="x@blaxx.test", cpf="11111111111")).status_code == 400)
    check("senha fraca → 400", c.post("/auth/register", json=dict(reg, email="y@blaxx.test", cpf=valid_cpf(223456789), password="123")).status_code == 400)
    check("sem aceite LGPD → 400", c.post("/auth/register", json=dict(reg, email="z@blaxx.test", cpf=valid_cpf(323456789), accept_lgpd=False)).status_code == 400)

    # ─── 2. LOGIN ───
    section("Login")
    check("login válido → 200", login("alpha@blaxx.test")[0] == 200)
    check("login por CPF → 200", c.post("/auth/login", json={"cpf": valid_cpf(900000002), "password": PWD}).status_code == 200)
    check("senha errada → 401", login("alpha@blaxx.test", "errada")[0] == 401)
    check("e-mail inexistente → 401", login("naoexiste@blaxx.test")[0] == 401)
    check("sem token em rota interna → 401/422", c.get("/wallet/").status_code in (401, 422))

    # ─── 3. RECUPERAÇÃO DE SENHA ───
    section("Recuperação de senha")
    check("forgot-password sempre 200 (anti-enum)", c.post("/auth/forgot-password", json={"email": "alpha@blaxx.test"}).status_code == 200)
    check("forgot inexistente → 200", c.post("/auth/forgot-password", json={"email": "naoexiste@blaxx.test"}).status_code == 200)
    check("reset com token inválido → 400", c.post("/auth/reset-password", json={"token": "x", "password": STRONG}).status_code == 400)

    # ─── 4. CARTEIRA / EXTRATO ───
    section("Carteira / Extrato")
    w = c.get("/wallet/", headers=H(ta)).get_json()
    check("saldo carrega", w.get("balance_pts") == 50000, f"bal={w.get('balance_pts')}")
    check("extrato lista", c.get("/wallet/transactions", headers=H(ta)).status_code == 200)

    # ─── 5. COMPRA PIX ───
    section("Compra PIX")
    b0 = bal(ta)
    ch = c.post("/pix/charge", json={"amount_brl": 90.0}, headers=H(ta)).get_json()
    check("cria charge → pontos esperados", ch.get("points_to_credit") == 1000, f"pts={ch.get('points_to_credit')}")
    c.post("/pix/simulate-payment", json={"charge_id": ch["id"]}, headers=H(ta))
    c.post("/pix/simulate-payment", json={"charge_id": ch["id"]}, headers=H(ta))  # duplicado
    check("crédito automático + idempotência (pago 2x)", (bal(ta) - b0) == 1000, f"delta={bal(ta)-b0}")
    ch2 = c.post("/pix/charge", json={"amount_brl": 50.0}, headers=H(ta)).get_json()
    with app.app_context():
        row = db.session.get(PixCharge, ch2["id"]); row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1); db.session.commit()
    st = c.get(f"/pix/charge/{ch2['id']}", headers=H(ta)).get_json()
    sim = c.post("/pix/simulate-payment", json={"charge_id": ch2["id"]}, headers=H(ta))
    check("charge expirada não paga", st.get("status") == "expired" and sim.status_code == 400, f"st={st.get('status')} sim={sim.status_code}")

    # ─── 6. ENVIO P2P ───
    section("Envio P2P")
    a0 = bal(ta); rb0 = bal(login("beta@blaxx.test")[1])
    r = c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 1000, "password": PWD}, headers=H(ta))
    tid = (r.get_json() or {}).get("id")
    tb = login("beta@blaxx.test")[1]
    check("envio ok: débito+crédito exatos", r.status_code == 201 and bal(ta) == a0 - 1000 and bal(tb) == rb0 + 1000, f"http={r.status_code}")
    check("destinatário inexistente → 400", c.post("/transfer/", json={"to": "ninguem@x.test", "amount_pts": 100, "password": PWD}, headers=H(ta)).status_code == 400)
    check("acima do saldo → 400", c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 999999999, "password": PWD}, headers=H(ta)).status_code == 400)
    check("abaixo do mínimo → 400", c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 50, "password": PWD}, headers=H(ta)).status_code == 400)
    check("senha errada → 400", c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 100, "password": "errada"}, headers=H(ta)).status_code == 400)
    check("para si mesmo → 400", c.post("/transfer/", json={"to": "alpha@blaxx.test", "amount_pts": 100, "password": PWD}, headers=H(ta)).status_code == 400)
    idem = {"Idempotency-Key": "qa-dup-1", **H(ta)}
    j1 = c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 300, "password": PWD}, headers=idem).get_json()
    j2 = c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 300, "password": PWD}, headers=idem).get_json()
    check("dedup por Idempotency-Key", j1.get("id") == j2.get("id"), "mesma tx")

    # ─── 7. RESGATE ───
    section("Resgate")
    ben = c.get("/benefits/", headers=H(ta)).get_json()["items"][0]
    rb = bal(ta)
    rr = c.post(f"/benefits/{ben['id']}/redeem", headers=H(ta))
    check("resgate debita pontos + gera voucher", rr.status_code in (200, 201) and (rb - bal(ta)) == ben["cost_pts"], f"http={rr.status_code}")
    check("voucher listado", len(c.get("/vouchers/", headers=H(ta)).get_json().get("items", [])) >= 1)
    check("resgate sem saldo → 402", c.post(f"/benefits/{ben['id']}/redeem", headers=H(login('zeta@blaxx.test')[1])).status_code == 402)

    # ─── 8. ADMIN ───
    section("Admin")
    check("admin lista usuários", c.get("/admin/users", headers=H(tadm)).status_code == 200)
    check("admin stats", c.get("/admin/stats", headers=H(tadm)).status_code == 200)
    check("admin export CSV", c.get("/admin/export/transactions.csv", headers=H(tadm)).status_code == 200)
    rv = c.post(f"/admin/transfers/{tid}/reverse", json={"reason": "estorno de teste qa"}, headers=H(tadm))
    check("estorno de transferência", rv.status_code == 200 and rv.get_json().get("reversed"), f"http={rv.status_code}")
    check("estorno idempotente", (c.post(f"/admin/transfers/{tid}/reverse", json={"reason": "de novo qa"}, headers=H(tadm)).get_json() or {}).get("already_reversed") is True)
    check("estorno exige justificativa", c.post(f"/admin/transfers/{tid}/reverse", json={"reason": "x"}, headers=H(tadm)).status_code == 400)
    with app.app_context():
        bid = db.session.query(User).filter_by(email="beta@blaxx.test").one().id
    check("bloquear usuário → login 403", c.patch(f"/admin/users/{bid}/status", json={"status": "suspended"}, headers=H(tadm)).status_code == 200 and login("beta@blaxx.test")[0] == 403)
    c.patch(f"/admin/users/{bid}/status", json={"status": "active"}, headers=H(tadm))
    check("não-admin barrado em /admin", c.get("/admin/users", headers=H(ta)).status_code == 403)
    # B14: alerta de transação suspeita (valor alto >= 30k)
    c.post("/transfer/", json={"to": "beta@blaxx.test", "amount_pts": 30000, "password": PWD}, headers=H(ta))
    _alerts = c.get("/admin/alerts", headers=H(tadm)).get_json().get("items", [])
    check("B14 alerta de transação suspeita gerado", any(x.get("event") == "suspicious_transfer" for x in _alerts), f"alertas={len(_alerts)}")
    check("B14 /admin/alerts é admin-only", c.get("/admin/alerts", headers=H(ta)).status_code == 403)

    # ─── 9. SEGURANÇA ───
    section("Segurança")
    chA = c.post("/pix/charge", json={"amount_brl": 20.0}, headers=H(ta)).get_json()
    check("cross-user: charge de outro → 404", c.get(f"/pix/charge/{chA['id']}", headers=H(login('zeta@blaxx.test')[1])).status_code == 404)
    vid = (c.get("/vouchers/", headers=H(ta)).get_json().get("items", [{}])[0] or {}).get("id", "x")
    check("cross-user: voucher de outro → 404", c.get(f"/vouchers/{vid}", headers=H(login('zeta@blaxx.test')[1])).status_code == 404)
    check("preço da compra é do backend (não manipulável)", c.post("/pix/charge", json={"package": "prime"}, headers=H(ta)).get_json().get("points_to_credit") == 12000)

    # ─── relatório ───
    npass = sum(1 for _, ok, _ in _results if ok)
    fails = [(n, g) for n, ok, g in _results if not ok]
    print("\n" + "=" * 64)
    print(f"  BLAXX QA LOOP · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  RESULTADO: {npass}/{len(_results)} PASS")
    if fails:
        print("  FALHAS:")
        for n, g in fails:
            print(f"    ✗ {n}  ({g})")
    else:
        print("  ✓ Tudo verde — liberado para publicar (parte automatizável).")
    print("  B13 (step-up 2FA) e B14 (alertas de fraude) implementados. Pendente: UI de ativação de 2FA.")
    print("  Não automatizável aqui: browsers/mobile/Lighthouse/webhook MP real/entrega de e-mail.")
    print("=" * 64)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
