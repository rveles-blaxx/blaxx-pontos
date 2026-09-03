#!/usr/bin/env bash
# Redeploy do backend Fly.io após mudanças no código Python.
# Uso: cd backend && ./redeploy.sh

set -euo pipefail
cd "$(dirname "$0")"

APP="blaxx-pontos-backend"

if ! command -v fly &> /dev/null; then
  echo "❌ fly CLI não está instalado. Instale: curl -L https://fly.io/install.sh | sh"
  exit 1
fi

if ! fly auth whoami &> /dev/null; then
  echo "⚠️  Você não está autenticado no Fly.io. Rode 'fly auth login' primeiro."
  exit 1
fi

echo "🚀 Redeploy do backend ($APP)..."
echo ""

# Confere que secrets do Google estão setados (essencial pro /auth/google)
echo "📋 Secrets configurados (filtro GOOGLE_*):"
fly secrets list --app "$APP" 2>/dev/null | grep -iE "(GOOGLE|NAME)" || echo "   (nenhum)"
echo ""

# Deploy com --no-cache pra GARANTIR que requirements.txt foi reinstalado
# (sem isso, mudanças em requirements.txt podem ser ignoradas se a camada
# Docker estiver cacheada — foi o caso de google-auth não ter sido instalado).
#
# --remote-only: usa builder remoto do Fly.io. Não precisa de Docker Desktop
# instalado/rodando localmente — funciona em qualquer Mac.
fly deploy --app "$APP" --no-cache --remote-only

echo ""
echo "✅ Deploy concluído."
echo ""
echo "🔍 Verifique que o CORS está respondendo a OPTIONS:"
echo "   curl -i -X OPTIONS https://$APP.fly.dev/auth/google \\"
echo "        -H 'Origin: https://blaxxpontos.netlify.app' \\"
echo "        -H 'Access-Control-Request-Method: POST'"
echo ""
echo "Esperado: status 204 + headers 'Access-Control-Allow-Origin' presente."
