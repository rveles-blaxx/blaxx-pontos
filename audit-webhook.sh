#!/usr/bin/env bash
# Auditoria de segurança do webhook PIX em PRODUÇÃO.
# Uso:  ./audit-webhook.sh
#
# Roda 6 testes contra https://blaxx-pontos-backend.fly.dev/pix/webhook
# e relata pass/fail. Não usa segredos — só testa que o endpoint rejeita
# requests inválidas conforme esperado.

set -uo pipefail

URL="${WEBHOOK_URL:-https://blaxx-pontos-backend.fly.dev/pix/webhook}"
PASS=0
FAIL=0
N=0

# helper
check() {
  local name="$1" expected="$2" actual="$3" body="${4:-}"
  N=$((N+1))
  if [ "$expected" = "$actual" ]; then
    echo "✅ [$N] $name → HTTP $actual (esperado $expected)"
    PASS=$((PASS+1))
  else
    echo "❌ [$N] $name → HTTP $actual (esperado $expected)"
    [ -n "$body" ] && echo "    Response: $body"
    FAIL=$((FAIL+1))
  fi
}

echo "🔐 Auditoria de segurança · $URL"
echo "============================================================="
echo ""

# ---------------------------------------------------------------
# Teste 1 — sem header x-signature (deve 401: assinatura ausente)
# ---------------------------------------------------------------
R=$(curl -s -o /tmp/audit-r1 -w "%{http_code}" -X POST "$URL" \
  -H "Content-Type: application/json" \
  -d '{"action":"payment.updated","data":{"id":"999"}}')
check "Sem x-signature" "401" "$R" "$(cat /tmp/audit-r1)"

# ---------------------------------------------------------------
# Teste 2 — x-signature inválida (deve 401: HMAC inválido)
# ---------------------------------------------------------------
R=$(curl -s -o /tmp/audit-r2 -w "%{http_code}" -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "x-request-id: audit-2" \
  -H "x-signature: ts=$(date +%s),v1=deadbeefdeadbeefdeadbeefdeadbeef00000000000000000000000000000000" \
  -d '{"action":"payment.updated","data":{"id":"999"}}')
check "x-signature inválida" "401" "$R" "$(cat /tmp/audit-r2)"

# ---------------------------------------------------------------
# Teste 3 — x-signature com ts antigo (anti-replay, deve 401)
# Mesmo se o HMAC fosse válido, ts de 1 hora atrás deve ser rejeitado.
# ---------------------------------------------------------------
OLD_TS=$(($(date +%s) - 3600))
R=$(curl -s -o /tmp/audit-r3 -w "%{http_code}" -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "x-request-id: audit-3" \
  -H "x-signature: ts=$OLD_TS,v1=abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789" \
  -d '{"action":"payment.updated","data":{"id":"999"}}')
check "x-signature com ts antigo (replay attack)" "401" "$R" "$(cat /tmp/audit-r3)"

# ---------------------------------------------------------------
# Teste 4 — body vazio + sem signature (deve 401)
# ---------------------------------------------------------------
R=$(curl -s -o /tmp/audit-r4 -w "%{http_code}" -X POST "$URL" \
  -H "Content-Type: application/json" \
  -d '{}')
check "Body vazio sem assinatura" "401" "$R" "$(cat /tmp/audit-r4)"

# ---------------------------------------------------------------
# Teste 5 — método errado (GET, deve 405)
# ---------------------------------------------------------------
R=$(curl -s -o /tmp/audit-r5 -w "%{http_code}" -X GET "$URL")
check "GET no webhook (não permitido)" "405" "$R" "$(cat /tmp/audit-r5)"

# ---------------------------------------------------------------
# Teste 6 — rate limit (61+ reqs em <60s deve 429)
# Manda 65 reqs sem signature. Espera-se ~60x 401 + ~5x 429.
# ---------------------------------------------------------------
echo ""
echo "Teste 6: rate limit (65 reqs sequenciais)..."
COUNT_401=0
COUNT_429=0
COUNT_OTHER=0
for i in $(seq 1 65); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d '{"audit":"rate-limit-test"}')
  case "$CODE" in
    401) COUNT_401=$((COUNT_401+1));;
    429) COUNT_429=$((COUNT_429+1));;
    *)   COUNT_OTHER=$((COUNT_OTHER+1));;
  esac
done
echo "   401 (rejeitado por assinatura): $COUNT_401"
echo "   429 (rate-limited): $COUNT_429"
echo "   Outros: $COUNT_OTHER"
if [ "$COUNT_429" -gt 0 ]; then
  echo "✅ [6] Rate limit ativo (≥1 request retornou 429)"
  PASS=$((PASS+1))
else
  echo "⚠️  [6] Rate limit NÃO acionado em 65 reqs. Verifique se limiter.limit('60 per minute') aplica corretamente."
  FAIL=$((FAIL+1))
fi
N=$((N+1))

# ---------------------------------------------------------------
echo ""
echo "============================================================="
echo "Resultado: $PASS/$N passaram · $FAIL falhas"
echo "============================================================="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
