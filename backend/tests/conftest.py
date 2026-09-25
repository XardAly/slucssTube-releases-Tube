"""Ambiente mínimo para testes unitários sem serviços externos reais."""

import os

os.environ.setdefault("JWT_SECRET", "test-secret-nunca-usar-em-producao")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("INTEGRITY_ENFORCED", "false")
