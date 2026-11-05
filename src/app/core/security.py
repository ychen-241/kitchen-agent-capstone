"""认证与安全：PBKDF2 口令哈希 + 自实现 HS256 JWT（零第三方依赖）。

生产替换点：换 PyJWT / Passlib 不影响调用方。
"""
import base64
import hashlib
import hmac
import json
import time

from . import config


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def secrets_hex(n: int = 8) -> str:
    import secrets as _s
    return _s.token_hex(n)


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def jwt_encode(payload: dict, expires_in: int = 8 * 3600) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    body = dict(payload)
    body["exp"] = int(time.time()) + expires_in
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(body).encode())}"
    sig = hmac.new(config.jwt_secret(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def jwt_decode(token: str) -> dict | None:
    try:
        head, body, sig = token.split(".")
        signing_input = f"{head}.{body}"
        expect = hmac.new(config.jwt_secret(), signing_input.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _b64url_decode(sig)):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def auth_user(auth_header: str | None) -> dict | None:
    """从 Authorization: Bearer <jwt> 解析用户。返回 {id, username, role} 或 None。"""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    payload = jwt_decode(auth_header[7:])
    if not payload:
        return None
    return {"id": payload.get("uid"), "username": payload.get("sub"), "role": payload.get("role")}
