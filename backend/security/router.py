"""Endpoints de challenge-response."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import AuthContext, get_auth_context
from backend.database.session import get_db
from sqlalchemy.orm import Session
from backend.security.challenge import consume_challenge, create_challenge

router = APIRouter(prefix="/security", tags=["security"])


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: str = Field(max_length=128)
    signature: str = Field(max_length=512, description="Assinatura Ed25519 do challenge (base64)")


@router.post("/challenge")
def issue_challenge(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    nonce = create_challenge(db, ctx.device.public_key)
    return {"challenge": nonce, "expires_in": 60}


@router.post("/verify")
def verify_challenge(
    data: VerifyRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """Endpoint de teste do fluxo. Operações reais consomem o challenge inline."""
    if not consume_challenge(db, data.challenge, ctx.device.public_key, data.signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge inválido ou expirado.")
    return {"verified": True}
