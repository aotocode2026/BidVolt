"""应用配置（.env 由 Secret Manager 注入，仓库内不保存明文）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 数据库
    database_url: str = "postgresql+asyncpg://bidvolt:bidvolt@127.0.0.1:5432/bidvolt"

    # 认证
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 120
    refresh_token_days: int = 30

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
