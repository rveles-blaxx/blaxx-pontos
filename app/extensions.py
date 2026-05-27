"""Extensões compartilhadas (instanciadas sem app, ligadas no factory)."""

from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
jwt = JWTManager()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],  # default global desligado; controle vem dos decorators
    storage_uri="memory://",   # sobrescrito no factory via app.config
)
