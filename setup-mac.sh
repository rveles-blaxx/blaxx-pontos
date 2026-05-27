#!/usr/bin/env bash
# Blaxx Pontos · setup local pra Mac
# Roda: cd backend && bash setup-mac.sh
set -e

echo "==> Verificando dependências do Mac..."
command -v python3 >/dev/null || { echo "✗ python3 não encontrado. Instale via Xcode CLT: xcode-select --install"; exit 1; }
echo "  ✓ python3: $(python3 --version)"

# Cria venv se não existir
if [ ! -d ".venv" ]; then
    echo "==> Criando .venv local..."
    python3 -m venv .venv
fi

# Ativa venv
source .venv/bin/activate

# Atualiza pip
python -m pip install --upgrade pip --quiet

# Instala deps
echo "==> Instalando dependências Python..."
pip install -r requirements.txt --quiet
pip install pytest pytest-cov --quiet

# .env se não existir
if [ ! -f ".env" ]; then
    echo "==> Criando .env a partir do .env.example..."
    cp .env.example .env
    # Gera SECRET_KEY e JWT_SECRET_KEY únicos
    SK=$(python -c "import secrets; print(secrets.token_hex(32))")
    JK=$(python -c "import secrets; print(secrets.token_hex(32))")
    sed -i.bak "s|SECRET_KEY=.*|SECRET_KEY=$SK|" .env
    sed -i.bak "s|JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$JK|" .env
    rm -f .env.bak
    echo "  ✓ Secrets únicos gerados"
fi

# Cria diretório instance se necessário
mkdir -p instance

# Roda migrations (db.create_all)
echo "==> Criando schema do banco..."
python -c "from app import create_app; create_app()"

# Popula seed (idempotente)
echo "==> Populando dados de teste..."
python seed.py

# Roda testes
echo ""
echo "==> Rodando testes de segurança Onda 1..."
DATABASE_URL="sqlite:///:memory:" MAILER=noop python -m pytest -v tests/test_auth_security.py || true

echo ""
echo "============================================================"
echo " ✓ Setup completo!"
echo "============================================================"
echo ""
echo " Para subir o servidor:"
echo "   source .venv/bin/activate"
echo "   python run.py"
echo ""
echo " Acesso:"
echo "   http://127.0.0.1:5050/health"
echo "   http://127.0.0.1:5050/app/      ← frontend renderer"
echo "   http://127.0.0.1:5050/blaxx/    ← frontend Netlify (local)"
echo ""
echo " Login demo:"
echo "   mariana@blaxx.com / 123456 (84.750 pts)"
echo "   lucas@blaxx.com   / 123456 (5.000 pts)"
echo ""
echo " Trocar para Mercado Pago (PIX real):"
echo "   Edite .env → PIX_PROVIDER=mercadopago + MP_ACCESS_TOKEN=..."
echo "   Reinicie o servidor."
