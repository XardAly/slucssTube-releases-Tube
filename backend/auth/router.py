"""Endpoints públicos de sessão (protegidos por rate limit de IP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.api.deps import AuthContext, client_ip, get_auth_context
from backend.auth import service
from backend.auth.tokens import create_upload_token
from backend.auth.schemas import RefreshRequest, SessionCreateRequest, TokenResponse
from backend.database.session import get_db
from backend.database.models import ApiRequest
from backend.security.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/session", tags=["session"])


@router.post("/create", response_model=TokenResponse)
async def create(
    data: SessionCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    enforce_rate_limit(db, device_id=None, ip=ip)
    db.add(ApiRequest(endpoint="/session/create", ip=ip))
    db.commit()
    return await service.create_session(db, data, ip)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    data: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    enforce_rate_limit(db, device_id=None, ip=ip)
    db.add(ApiRequest(endpoint="/session/refresh", ip=ip))
    db.commit()
    return service.refresh_session(db, data.refresh_token)


@router.post("/upload-token")
def upload_token(ctx: AuthContext = Depends(get_auth_context)):
    """Token restrito para o site enviar o vídeo direto à API (sem proxy)."""
    token, ttl = create_upload_token(ctx.device.id)
    return {"token": token, "expires_in": ttl}


@router.delete("/revoke", status_code=204)
def revoke(data: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    enforce_rate_limit(db, device_id=None, ip=ip)
    db.add(ApiRequest(endpoint="/session/revoke", ip=ip))
    db.commit()
    service.revoke_session(db, data.refresh_token)
