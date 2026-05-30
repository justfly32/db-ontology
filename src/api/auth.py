"""
Phase 7-2: RBAC (Role-Based Access Control) for the Ontology API
JWT 기반 인증 + 역할 기반 권한 관리
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Optional

logger = logging.getLogger(__name__)


class Role(str, Enum):
    VIEWER = "viewer"       # 읽기 전용
    ANALYST = "analyst"     # 분석 실행 + 읽기
    ADMIN = "admin"         # 모든 권능
    SERVICE = "service"     # 서비스 간 통신 (cron, webhook)


# ── 권한 매트릭스 ─────────────────────────────────────
PERMISSIONS: dict[Role, set[str]] = {
    Role.VIEWER: {
        "graph:read", "table:read", "column:read",
        "relationship:read", "search:read", "drift:read",
    },
    Role.ANALYST: {
        "graph:read", "table:read", "column:read",
        "relationship:read", "search:read", "drift:read",
        "analysis:run", "export:graphml", "export:json",
        "collect:schema",
    },
    Role.ADMIN: {
        "graph:read", "graph:write", "table:read", "table:write",
        "column:read", "column:write", "relationship:read", "relationship:write",
        "search:read", "drift:read", "drift:write",
        "analysis:run", "export:graphml", "export:json",
        "collect:schema", "user:manage", "config:write",
        "notification:send",
    },
    Role.SERVICE: {
        "graph:read", "table:read", "column:read",
        "relationship:read", "drift:read", "drift:write",
        "collect:schema", "notification:send",
    },
}


@dataclass
class User:
    user_id: str
    username: str
    email: str
    role: Role
    password_hash: str = ""
    is_active: bool = True
    created_at: str = ""
    api_key: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class TokenPayload:
    sub: str          # user_id
    username: str
    role: str
    exp: float        # expiry timestamp
    iat: float        # issued-at timestamp
    jti: str          # JWT ID


class UserStore:
    """인메모리 사용자 저장소 (운영에서는 DB/Redis 사용)"""

    def __init__(self):
        self._users: dict[str, User] = {}       # user_id → User
        self._by_username: dict[str, str] = {}  # username → user_id
        self._by_api_key: dict[str, str] = {}   # api_key → user_id
        self._revoked_tokens: set[str] = set()

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: Role = Role.VIEWER,
    ) -> User:
        if username in self._by_username:
            raise ValueError(f"Username '{username}' already exists")

        user_id = secrets.token_hex(8)
        password_hash = self._hash_password(password)
        api_key = f"onto_{secrets.token_hex(24)}"

        user = User(
            user_id=user_id,
            username=username,
            email=email,
            role=role,
            password_hash=password_hash,
            is_active=True,
            created_at=datetime.utcnow().isoformat(),
            api_key=api_key,
        )
        self._users[user_id] = user
        self._by_username[username] = user_id
        self._by_api_key[api_key] = user_id
        logger.info(f"Created user: {username} (role={role.value})")
        return user

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """username + password 인증"""
        uid = self._by_username.get(username)
        if not uid:
            return None
        user = self._users.get(uid)
        if not user or not user.is_active:
            return None
        if not self._verify_password(password, user.password_hash):
            return None
        return user

    def get_by_api_key(self, api_key: str) -> Optional[User]:
        """API Key 인증"""
        uid = self._by_api_key.get(api_key)
        if uid:
            user = self._users.get(uid)
            if user and user.is_active:
                return user
        return None

    def get_by_id(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def revoke_token(self, jti: str):
        self._revoked_tokens.add(jti)

    def is_token_revoked(self, jti: str) -> bool:
        return jti in self._revoked_tokens

    def delete_user(self, user_id: str) -> bool:
        user = self._users.pop(user_id, None)
        if user:
            self._by_username.pop(user.username, None)
            self._by_api_key.pop(user.api_key, None)
            return True
        return False

    def list_users(self) -> list[User]:
        return list(self._users.values())

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return f"{salt}${pw_hash.hex()}"

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        try:
            salt, hash_hex = stored_hash.split("$", 1)
            pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
            return hmac.compare_digest(pw_hash.hex(), hash_hex)
        except (ValueError, AttributeError):
            return False


class JWTManager:
    """단순 JWT 구현 (PyJWT 없이 HMAC-SHA256 기반)"""

    def __init__(self, secret_key: str, algorithm: str = "HS256", expiry_hours: int = 24):
        self.secret = secret_key.encode()
        self.algorithm = algorithm
        self.expiry_hours = expiry_hours

    def create_token(self, user: User) -> str:
        """사용자 정보로 JWT 생성"""
        now = time.time()
        payload = TokenPayload(
            sub=user.user_id,
            username=user.username,
            role=user.role.value,
            exp=now + self.expiry_hours * 3600,
            iat=now,
            jti=secrets.token_hex(16),
        )
        header = json.dumps({"alg": self.algorithm, "typ": "JWT"}, separators=(",", ":"))
        body = json.dumps({
            "sub": payload.sub,
            "username": payload.username,
            "role": payload.role,
            "exp": payload.exp,
            "iat": payload.iat,
            "jti": payload.jti,
        }, separators=(",", ":"))

        h = self._b64url(header.encode())
        b = self._b64url(body.encode())
        sig = self._b64url(self._sign(f"{h}.{b}".encode()))

        return f"{h}.{b}.{sig}"

    def verify_token(self, token: str, user_store: UserStore) -> Optional[TokenPayload]:
        """JWT 검증"""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            h, b, sig = parts

            # 서명 검증
            expected_sig = self._b64url(self._sign(f"{h}.{b}".encode()))
            if not hmac.compare_digest(sig, expected_sig):
                return None

            payload = json.loads(self._b64url_decode(b))

            # 만료 확인
            if payload.get("exp", 0) < time.time():
                return None

            # 폐기 확인
            if user_store.is_token_revoked(payload.get("jti", "")):
                return None

            return TokenPayload(
                sub=payload["sub"],
                username=payload["username"],
                role=payload["role"],
                exp=payload["exp"],
                iat=payload["iat"],
                jti=payload["jti"],
            )
        except Exception as e:
            logger.warning(f"Token verification failed: {e}")
            return None

    def _sign(self, data: bytes) -> bytes:
        return hmac.new(self.secret, data, hashlib.sha256).digest()

    @staticmethod
    def _b64url(data: bytes) -> str:
        import base64
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        import base64
        padding = 4 - len(s) % 4
        if padding != 4:
            s += "=" * padding
        return base64.urlsafe_b64decode(s)


class RBACMiddleware:
    """FastAPI 의존성 주입용 RBAC 헬퍼"""

    def __init__(
        self,
        user_store: UserStore,
        jwt_manager: JWTManager,
    ):
        self.user_store = user_store
        self.jwt = jwt_manager

    def get_current_user(self, authorization: Optional[str] = None, x_api_key: Optional[str] = None) -> User:
        """Authorization 헤더 또는 API Key로 사용자 식별"""
        # API Key 우선
        if x_api_key:
            user = self.user_store.get_by_api_key(x_api_key)
            if user:
                return user

        # JWT Bearer 토큰
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            payload = self.jwt.verify_token(token, self.user_store)
            if payload:
                user = self.user_store.get_by_id(payload.sub)
                if user and user.is_active:
                    return user

        raise PermissionError("Invalid credentials")

    def require_permission(self, user: User, permission: str) -> bool:
        """사용자가 특정 권한을 가지고 있는지 확인"""
        allowed = PERMISSIONS.get(user.role, set())
        if permission not in allowed:
            raise PermissionError(
                f"Permission denied: '{permission}' required. User role: {user.role.value}"
            )
        return True

    def require_permissions(self, user: User, permissions: list[str]) -> bool:
        """여러 권한 모두 확인"""
        for p in permissions:
            self.require_permission(user, p)
        return True


# ── FastAPI 의존성 ────────────────────────────────────

# 전역 인스턴스 (create_app에서 교체 가능)
_default_secret = os.environ.get("JWT_SECRET", secrets.token_hex(32))
_user_store = UserStore()
_jwt_manager = JWTManager(secret_key=_default_secret)
rbac = RBACMiddleware(_user_store, _jwt_manager)


def get_user_store() -> UserStore:
    return _user_store


def get_jwt_manager() -> JWTManager:
    return _jwt_manager


def get_rbac() -> RBACMiddleware:
    return rbac


def create_default_admin():
    """기본 관리자 계정 생성"""
    store = get_user_store()
    try:
        admin = store.create_user(
            username="admin",
            email="admin@ontology.local",
            password=os.environ.get("ADMIN_PASSWORD", "change-me-please"),
            role=Role.ADMIN,
        )
        logger.info(f"Default admin created: api_key={admin.api_key[:12]}...")
        return admin
    except ValueError:
        return store.authenticate("admin", os.environ.get("ADMIN_PASSWORD", "change-me-please"))
