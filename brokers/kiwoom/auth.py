"""
Kiwoom REST API authentication client.

Data-only: token management and auth headers.
No order functions. No key/token values in logs.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.kiwoom.com"

_FIXED_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 KiwoomOpenAPIClient/1.0",
}

_TOKEN_REISSUE_GUARD_SEC = 30.0
_TOKEN_RETRY_DELAYS_SEC = (1.0, 2.0, 5.0)


class KiwoomAuthClient:
    """Token cache for Kiwoom REST OpenAPI. Re-issues before expiry automatically."""

    def __init__(self, app_key: str, app_secret: str, env: str = "real") -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._env = env
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._last_issue_attempt_at: float = 0.0
        self._cache_path = self._resolve_cache_path()
        self._load_cached_token()

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "KiwoomAuthClient":
        if env_path is None:
            env_path = Path(__file__).resolve().parents[2] / "config" / "kiwoom.env"
        load_dotenv(env_path, override=False)
        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
        app_key = os.environ.get("KIWOOM_APP_KEY") or os.environ.get("KIWOOM_APPKEY", "")
        app_secret = os.environ.get("KIWOOM_APP_SECRET") or os.environ.get("KIWOOM_APPSECRET", "")
        env = os.environ.get("KIWOOM_ENV", "real")
        if not app_key or not app_secret:
            raise RuntimeError(
                f"KIWOOM_APP_KEY(KIWOOM_APPKEY) / KIWOOM_APP_SECRET(KIWOOM_APPSECRET) not found near {env_path}"
            )
        return cls(app_key=app_key, app_secret=app_secret, env=env)

    def _is_expired(self) -> bool:
        if not self._token or not self._expires_at:
            return True
        return datetime.now() >= self._expires_at - timedelta(minutes=5)

    def _resolve_cache_path(self) -> Path:
        root = Path(__file__).resolve().parents[2]
        cache_dir = root / "data"
        cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = "mock" if str(self._env).lower().startswith("mock") else "real"
        return cache_dir / f"kiwoom_token_cache_{suffix}.txt"

    def _load_cached_token(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            raw = self._cache_path.read_text(encoding="utf-8").splitlines()
            if len(raw) < 2:
                return
            token = raw[0].strip()
            expires_at = datetime.fromisoformat(raw[1].strip())
            if not token:
                return
            if datetime.now() >= expires_at - timedelta(minutes=5):
                return
            self._token = token
            self._expires_at = expires_at
            logger.info(
                "Kiwoom 캐시 토큰 재사용 (만료: %s)",
                self._expires_at.strftime("%Y-%m-%d %H:%M"),
            )
        except Exception as exc:
            logger.warning("Kiwoom 토큰 캐시 로드 실패: %s", exc)

    def _persist_token(self) -> None:
        if not self._token or not self._expires_at:
            return
        try:
            self._cache_path.write_text(
                f"{self._token}\n{self._expires_at.isoformat()}\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Kiwoom 토큰 캐시 저장 실패: %s", exc)

    def _guard_reissue_interval(self) -> None:
        elapsed = time.monotonic() - self._last_issue_attempt_at
        if self._last_issue_attempt_at <= 0 or elapsed >= _TOKEN_REISSUE_GUARD_SEC:
            return
        wait_sec = _TOKEN_REISSUE_GUARD_SEC - elapsed
        logger.info("Kiwoom 토큰 재발급 간격 대기 %.1fs", wait_sec)
        time.sleep(wait_sec)

    def _issue_token(self) -> None:
        base = os.environ.get("KIWOOM_BASE_URL", _DEFAULT_BASE_URL)
        url = base + "/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        self._guard_reissue_interval()
        last_exc: Exception | None = None
        for attempt, delay_sec in enumerate((0.0, *_TOKEN_RETRY_DELAYS_SEC), start=1):
            if delay_sec > 0:
                logger.warning("Kiwoom 토큰 재시도 %d회차 전 %.1fs 대기", attempt, delay_sec)
                time.sleep(delay_sec)
            self._last_issue_attempt_at = time.monotonic()
            try:
                resp = requests.post(url, json=payload, headers=_FIXED_HEADERS, timeout=15)
                if resp.status_code == 429:
                    last_exc = RuntimeError("Kiwoom 토큰 요청 제한(HTTP 429)")
                    logger.warning("Kiwoom 토큰 요청 제한 감지 (attempt=%d)", attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as exc:
                last_exc = exc
        else:
            raise RuntimeError(f"Kiwoom 토큰 요청 실패: {last_exc}") from last_exc

        if data.get("return_code") != 0:
            raise RuntimeError(f"Kiwoom 토큰 거부: {data.get('return_msg')}")

        raw_token = data.get("token") or data.get("access_token")
        if not raw_token:
            raise RuntimeError(
                f"Kiwoom 토큰 응답에 token 필드 없음. 키: {list(data.keys())}"
            )

        self._token = raw_token

        expires_str = str(data.get("expires_dt", ""))
        try:
            if len(expires_str) >= 14:
                self._expires_at = datetime.strptime(expires_str[:14], "%Y%m%d%H%M%S")
            else:
                self._expires_at = datetime.now() + timedelta(hours=12)
        except ValueError:
            self._expires_at = datetime.now() + timedelta(hours=12)

        self._persist_token()
        logger.info("✅ Kiwoom 토큰 발급 완료 (만료: %s)", self._expires_at.strftime("%Y-%m-%d %H:%M"))

    def token(self) -> str:
        if self._is_expired():
            self._issue_token()
        assert self._token is not None
        return self._token

    def auth_headers(self, extra: dict | None = None) -> dict:
        headers = dict(_FIXED_HEADERS)
        headers["Authorization"] = f"Bearer {self.token()}"
        if extra:
            headers.update(extra)
        return headers
