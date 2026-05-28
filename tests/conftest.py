"""Adiciona a raiz do backend ao sys.path pra os testes acharem `app`.

Sem este arquivo: `pytest tests/` da ModuleNotFoundError: No module named 'app'
porque o cwd do pytest nao entra no PYTHONPATH automaticamente.

Equivalente a rodar `python -m pytest` (que adiciona cwd ao sys.path).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
