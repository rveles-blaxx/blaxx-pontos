"""Entrypoint do backend Flask embarcado no app Electron Blaxx Pontos.

Por padrão escuta em 127.0.0.1:5050 (porta usada pelo Electron main.js).
A porta pode ser sobrescrita via env var BLAXX_BACKEND_PORT.
"""

import os

# ---- .env local (SOMENTE dev/test) ---- #
# Conveniência para homologar providers (Asaas/Stripe) sem exportar variável a
# cada terminal. Guardado por FLASK_ENV de propósito: em PRODUÇÃO o gunicorn
# importa este módulo (`run:app`), e ler um .env do disco lá poderia
# sobrescrever silenciosamente as env vars do painel do Render — que são a
# fonte de verdade. Por isso: em produção, nunca.
if (os.environ.get("FLASK_ENV") or "production").lower() in ("development", "test"):
    try:
        from dotenv import load_dotenv

        _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(_env_path):
            # override=False: variável já exportada no shell vence o arquivo.
            load_dotenv(_env_path, override=False)
            print(f"[dev] .env carregado de {_env_path}")
    except ImportError:
        pass

from app import create_app

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("BLAXX_BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("BLAXX_BACKEND_PORT", os.environ.get("PORT", "5050")))
    # debug=False para evitar reloader spawnar segundo processo dentro do Electron
    app.run(host=host, port=port, debug=False, use_reloader=False)
