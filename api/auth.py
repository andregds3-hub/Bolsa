"""
Autenticação do MVP: senha com scrypt (stdlib) e token de sessão assinado (HMAC-SHA256)
entregue em cookie HttpOnly + SameSite=Lax.

Produção: Keycloak/OIDC no padrão BFF (OAuth social, MFA, recuperação de senha por
e-mail, bloqueio por força bruta, rotação de refresh token).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

SECRET = (os.environ.get("SIM_SECRET") or secrets.token_hex(32)).encode()
SESSION_TTL_S = 8 * 3600
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
_COMMON = {"123456789012", "senha1234567", "password1234", "qwertyuiop12", "abcdefgh1234"}
_revoked: set[str] = set()  # jti de sessões encerradas (produção: Redis com TTL)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return algo == "scrypt" and hmac.compare_digest(dk.hex(), dk_hex)


def password_problems(password: str, email: str = "") -> list[str]:
    problems = []
    if len(password) < 12:
        problems.append("mínimo de 12 caracteres")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        problems.append("combine letras e números")
    local = email.split("@")[0].lower()
    if password.lower() in _COMMON or (len(local) >= 4 and local in password.lower()):
        problems.append("senha muito previsível")
    return problems


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())


def issue_token(user_id: str) -> str:
    claims = {"sub": user_id, "exp": int(time.time()) + SESSION_TTL_S, "jti": secrets.token_hex(8)}
    payload = _b64(json.dumps(claims).encode())
    return f"{payload}.{_sign(payload)}"


def _claims(token: Optional[str]) -> Optional[dict]:
    if not token or token.count(".") != 1:
        return None
    payload, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except ValueError:
        return None
    if claims.get("exp", 0) < time.time() or claims.get("jti") in _revoked:
        return None
    return claims


def read_token(token: Optional[str]) -> Optional[str]:
    claims = _claims(token)
    return claims.get("sub") if claims else None


def revoke_token(token: Optional[str]) -> None:
    claims = _claims(token)
    if claims:
        _revoked.add(claims["jti"])
