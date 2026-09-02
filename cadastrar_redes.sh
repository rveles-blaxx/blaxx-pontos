#!/bin/bash
# Cadastra as 3 redes B2B em produção pelos endpoints admin.
#
# Existe porque o serviço está no plano `free` do Render, que NÃO tem Shell —
# então `python seed_merchants.py` não é executável lá. Este script faz o mesmo
# pela API. Idempotente: CNPJ repetido devolve 409 e o script segue.
#
#   chmod +x cadastrar_redes.sh && ./cadastrar_redes.sh
#
# A senha é lida sem eco e não fica no histórico do shell.
set -euo pipefail
API="https://blaxx-pontos-exe.onrender.com"

read -rp "e-mail do admin: " EMAIL
read -rsp "senha: " SENHA; echo

TOKEN=$(curl -s -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$SENHA\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
[ -n "$TOKEN" ] || { echo "login falhou"; exit 1; }
echo "login ok"
unset SENHA

# nome|cnpj|vertical|centavos que o cliente gasta por ponto|centavos que a rede paga|teto
REDES=(
  "Posto Girassol|11111111000191|posto|1000|10|500"
  "Supermercado Pilar|22222222000172|supermercado|500|10|1000"
  "Farmácia Aurora|33333333000153|farmacia|300|10|800"
)

for linha in "${REDES[@]}"; do
  IFS='|' read -r nome cnpj vert acc bill cap <<< "$linha"
  echo
  echo "== $nome"
  resp=$(curl -s -X POST "$API/admin/merchants" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d "{\"name\":\"$nome\",\"cnpj\":\"$cnpj\",
    \"vertical\":\"$vert\",\"accrual_cents_per_point\":$acc,
    \"bill_cents_per_point\":$bill,\"max_points_per_tx\":$cap}")
  mid=$(printf '%s' "$resp" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("merchant",{}).get("id",""))
except Exception: print("")')
  if [ -z "$mid" ]; then
    echo "  já existe ou erro: $(printf '%s' "$resp" | head -c 120)"
    continue
  fi
  chave=$(curl -s -X POST "$API/admin/merchants/$mid/keys" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d '{"label":"PDV principal"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key",""))')
  echo "  id:    $mid"
  echo "  CHAVE: $chave"
  echo "         ^ guarde AGORA; não é recuperável"
done

echo
echo "Conferir:  curl -s -H 'Authorization: Bearer \$TOKEN' $API/admin/merchants | python3 -m json.tool"
