"""Entrypoint do backend Flask embarcado no app Electron Blaxx Pontos.

Por padrão escuta em 127.0.0.1:5050 (porta usada pelo Electron main.js).
A porta pode ser sobrescrita via env var BLAXX_BACKEND_PORT.
"""

import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("BLAXX_BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("BLAXX_BACKEND_PORT", os.environ.get("PORT", "5050")))
    # debug=False para evitar reloader spawnar segundo processo dentro do Electron
    app.run(host=host, port=port, debug=False, use_reloader=False)
