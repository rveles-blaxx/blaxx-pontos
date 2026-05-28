"""Sprint 5 (S5-5) · Swagger UI servindo openapi.yaml.

Endpoints:
  GET /docs/         → Swagger UI HTML standalone
  GET /docs/openapi.yaml → spec OpenAPI 3.1
"""

from __future__ import annotations

import os

from flask import Blueprint, Response, send_from_directory

bp = Blueprint("docs", __name__)

# Caminho absoluto pro openapi.yaml na raiz do backend (lado de app/)
BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


SWAGGER_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>Blaxx Pontos API · Swagger</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>📘</text></svg>">
  <style>
    body { margin: 0; }
    .topbar { background:#0A0A0A !important; }
    .swagger-ui .topbar .download-url-wrapper input[type=text] { border-color:#C6F432 }
  </style>
</head>
<body>
  <div id="swagger"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js" crossorigin></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js" crossorigin></script>
  <script>
    window.onload = function () {
      window.ui = SwaggerUIBundle({
        url: '/docs/openapi.yaml',
        dom_id: '#swagger',
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
        layout: 'StandaloneLayout',
        tryItOutEnabled: true,
        persistAuthorization: true,
      });
    };
  </script>
</body>
</html>
"""


@bp.get("/")
def index():
    return Response(SWAGGER_HTML, mimetype="text/html; charset=utf-8")


@bp.get("/openapi.yaml")
def openapi_yaml():
    return send_from_directory(BACKEND_ROOT, "openapi.yaml",
                                mimetype="application/yaml")
