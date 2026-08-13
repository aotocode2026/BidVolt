"""应用配置（.env 由 Secret Manager 注入，仓库内不保存明文）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 生产模式拒绝的弱默认值（Issue #3：生产漏配必须 fail-fast，不能携带弱默认值启动）
WEAK_DB_URL = "postgresql+asyncpg://bidvolt:bidvolt@127.0.0.1:5432/bidvolt"
WEAK_JWT_SECRETS = ("dev-only-secret-change-me", "change-me", "changeme", "secret")
WEAK_PASSWORDS = ("bidvolt", "password", "changeme", "change-me", "123456", "postgres")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 运行环境：dev（默认，允许弱默认值便于本地开发）/ production（fail-fast）
    bidvolt_env: str = "dev"

    # 数据库
    database_url: str = WEAK_DB_URL

    # 认证
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 120
    refresh_token_days: int = 30
    # MCP ↔ 后端内部认证令牌（Hermes 服务账号；生产 ≥32 位随机串）
    bidvolt_internal_token: str = ""

    # 存储/备份（/data 为平台外挂持久卷）
    storage_root: str = "/data/appdata"
    backup_root: str = "/data/backups"

    # 云模型/搜索门禁（P1：DATA_CLASSIFICATION_CONFIRMED=1 前强制关闭）
    data_classification_confirmed: int = 0
    cloud_llm_enabled: int = 0
    search_enabled: int = 0
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.chat/v1"
    minimax_model: str = "MiniMax-Text-01"
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_vl_model: str = "qwen-vl-max"
    anysearch_key: str = ""
    anysearch_base_url: str = "https://api.anysearch.com/mcp"  # AnySearch 官方端点（JSON-RPC 2.0）
    search_mode: str = "mock"  # mock（本地合成，无出网）/ anysearch（真实出网，无 Key 走匿名额度）
    http_proxy: str = ""  # 出站代理（搜索/外部调用）；空则直连白名单域名

    # 业务端口（容器内 8123，公网 28123）
    bind_host: str = "0.0.0.0"
    bind_port: int = 8123

    # 文件安全（M2，P1）
    max_upload_bytes: int = 200 * 1024 * 1024
    virus_scan_required: bool = False  # 生产置 True：ClamAV 不可用则 fail-closed

    @property
    def cloud_features_locked(self) -> bool:
        """数据分级未确认时，云模型/搜索强制关闭（fail-closed）。"""
        return self.data_classification_confirmed != 1

    @model_validator(mode="after")
    def _reject_weak_production_secrets(self) -> Settings:
        """生产模式 fail-fast：拒绝弱默认数据库口令/JWT 密钥/内部令牌与薄弱配置。

        Issue #3 门禁：生产漏配时不得携带开发默认值启动。仅 BIDVOLT_ENV=production
        （或 prod）时生效，本地开发/测试保持宽松。
        """
        if self.bidvolt_env.strip().lower() not in ("production", "prod"):
            return self
        errors: list[str] = []
        if self.database_url == WEAK_DB_URL:
            errors.append("DATABASE_URL 仍为开发默认值（bidvolt:bidvolt），生产禁止")
        elif self._db_password_weak():
            if self.database_url.startswith("sqlite"):
                errors.append("生产禁止 SQLite 主库（无 RLS 租户隔离，须使用 PostgreSQL）")
            else:
                errors.append("DATABASE_URL 使用弱数据库口令，生产禁止")
        if len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET 长度不足 32 位，生产禁止")
        if any(w in self.jwt_secret.strip().lower() for w in WEAK_JWT_SECRETS):
            errors.append("JWT_SECRET 使用了占位/示例值（change-me 等），生产禁止")
        if not self.bidvolt_internal_token:
            errors.append("BIDVOLT_INTERNAL_TOKEN 未设置，生产禁止")
        elif len(self.bidvolt_internal_token) < 32:
            errors.append("BIDVOLT_INTERNAL_TOKEN 长度不足 32 位，生产禁止")
        elif any(w in self.bidvolt_internal_token.strip().lower() for w in WEAK_JWT_SECRETS):
            errors.append("BIDVOLT_INTERNAL_TOKEN 使用了占位/示例值，生产禁止")
        if not self.virus_scan_required:
            errors.append("VIRUS_SCAN_REQUIRED 必须为 1（ClamAV fail-closed），生产禁止关闭")
        if errors:
            raise ValueError(
                "生产配置校验失败，拒绝启动（BIDVOLT_ENV=production）：\n- " + "\n- ".join(errors)
            )
        return self

    def _db_password_weak(self) -> bool:
        try:
            from sqlalchemy.engine import make_url

            password = make_url(self.database_url).password
        except Exception:  # noqa: BLE001
            return True
        if not password:
            return True
        return password.lower() in WEAK_PASSWORDS


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
