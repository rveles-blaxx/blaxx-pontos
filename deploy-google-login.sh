#!/usr/bin/env bash
# Deploy Google Login — configura secrets, sobe backend e disparado migration.
#
# Uso: cd /Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx\ Pontos/blaxx_app/backend
#      ./deploy-google-login.sh

set -euo pipefail
cd "$(dirname "$0")"

APP="blaxx-pontos-backend"
WEB_CLIENT_ID="105341431878-tj5vi2is40n8gbugugj9bgvi2b67v0el.apps.googleusercontent.com"
IOS_CLIENT_ID="105341431878-3msc2p3tjk3p5ro6i34d0b0qks3nf9dj.apps.googleusercontent.com"

echo "🔧 Configurando Google OAuth secrets na Fly.io (app: $APP)..."
echo ""

# 1) Verificar que fly CLI está instalado
if ! command -v fly &> /dev/null; then
  echo "❌ fly CLI não está instalado. Instale: curl -L https://fly.io/install.sh | sh"
  exit 1
fi

# 2) Verificar autenticação
if ! fly auth whoami &> /dev/null; then
  echo "⚠️  Você não está autenticado no Fly.io. Rode 'fly auth login' primeiro."
  exit 1
fi

# 3) Set secrets — isso já dispara um redeploy do backend automaticamente
echo "📡 fly secrets set GOOGLE_WEB_CLIENT_ID + GOOGLE_IOS_CLIENT_ID..."
fly secrets set \
  "GOOGLE_WEB_CLIENT_ID=$WEB_CLIENT_ID" \
  "GOOGLE_IOS_CLIENT_ID=$IOS_CLIENT_ID" \
  --app "$APP"

echo ""
echo "✅ Secrets configurados. Backend está fazendo redeploy automático."
echo ""
echo "📋 PRÓXIMO PASSO MANUAL (única coisa que falta):"
echo "   Rode a migration no Neon — adiciona coluna google_sub na tabela users."
echo "   1. Abra: https://console.neon.tech"
echo "   2. SQL Editor"
echo "   3. Cole o conteúdo de: migrations/2026-05-25_google_oauth.sql"
echo "   4. Run"
echo ""
echo "🌐 Depois desse passo:"
echo "   - Login Google funcionando na web (após deploy do Netlify)"
echo "   - Login Google funcionando no Mac/iOS (após build do Xcode)"
echo ""
echo "Acompanhe o redeploy do backend em:"
echo "   https://fly.io/apps/$APP/monitoring"
