"""
Application settings — loaded from environment variables / .env file.
Uses pydantic-settings for type-safe config.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


# Marketplace ID → SP-API region 對照表
MARKETPLACE_REGION_MAP: dict[str, str] = {
    # North America
    "ATVPDKIKX0DER": "na",  # US
    "A2EUQ1WTGCTBG2": "na",  # CA
    "A1AM78C64UM0Y8": "na",  # MX
    # Europe
    "A1F83G8C2ARO7P": "eu",  # UK
    "A1PA6795UKMFR9": "eu",  # DE
    "A13V1IB3VIYZZH": "eu",  # FR
    "APJ6JRA9NG5V4":  "eu",  # IT
    "A1RKKUPIHCS9HS": "eu",  # ES
    "A1805IZSGTT6HS": "eu",  # SG (eu endpoint)
    # Far East
    "A1VC38T7YXB528": "fe",  # JP
    "A39IBJ37TRP1C6": "fe",  # AU
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── SP-API ────────────────────────────────────────────────────────────────
    # 主 refresh_token（也是 NA region 用的，向後相容）
    SP_API_REFRESH_TOKEN: str    = Field(..., description="LWA Refresh Token (NA region)")
    SP_API_CLIENT_ID: str        = Field(..., description="LWA Client ID")
    SP_API_CLIENT_SECRET: str    = Field(..., description="LWA Client Secret")

    # 各 region 專屬 refresh_token（EU / FE 都是各自獨立授權的）
    # 若某個 region 沒填 → fallback 用 SP_API_REFRESH_TOKEN（NA）
    SP_API_REFRESH_TOKEN_EU: str = Field("", description="LWA Refresh Token (EU region, UK/DE/...)")
    SP_API_REFRESH_TOKEN_FE: str = Field("", description="LWA Refresh Token (FE region, JP/AU/SG)")

    # 支援多 marketplace，逗號分隔
    SP_API_MARKETPLACE_IDS: list[str] = Field(
        default=["ATVPDKIKX0DER"],
        description="Comma-separated Marketplace IDs"
    )

    @field_validator("SP_API_MARKETPLACE_IDS", mode="before")
    @classmethod
    def parse_marketplace_ids(cls, v):
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v

    def region_for(self, marketplace_id: str) -> str:
        """回傳該 marketplace 對應的 SP-API region。"""
        return MARKETPLACE_REGION_MAP.get(marketplace_id, "na")

    def refresh_token_for(self, marketplace_id: str) -> str:
        """
        依 marketplace 選對應 region 的 refresh_token。
        - NA marketplaces → SP_API_REFRESH_TOKEN
        - EU marketplaces → SP_API_REFRESH_TOKEN_EU（沒填則 fallback NA）
        - FE marketplaces → SP_API_REFRESH_TOKEN_FE（沒填則 fallback NA）
        """
        region = self.region_for(marketplace_id)
        if region == "eu" and self.SP_API_REFRESH_TOKEN_EU:
            return self.SP_API_REFRESH_TOKEN_EU
        if region == "fe" and self.SP_API_REFRESH_TOKEN_FE:
            return self.SP_API_REFRESH_TOKEN_FE
        return self.SP_API_REFRESH_TOKEN

    # ── AWS (optional, for IAM role) ──────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str     = Field("", description="AWS Access Key (optional)")
    AWS_SECRET_ACCESS_KEY: str = Field("", description="AWS Secret Key (optional)")
    AWS_ROLE_ARN: str          = Field("", description="IAM Role ARN (optional)")

    # ── Ads API ───────────────────────────────────────────────────────────────
    ADS_CLIENT_ID: str      = Field("", description="Ads API Client ID")
    ADS_CLIENT_SECRET: str  = Field("", description="Ads API Client Secret")
    ADS_REFRESH_TOKEN: str  = Field("", description="Ads API Refresh Token")
    ADS_PROFILE_ID: str     = Field("", description="Ads Profile ID")

    # ── DuckDB ────────────────────────────────────────────────────────────────
    DUCKDB_PATH: str = Field("/data/sp_api.duckdb", description="DuckDB file path")

    # ── ETL Behaviour ─────────────────────────────────────────────────────────
    ETL_LOG_LEVEL: str        = Field("INFO", description="Log level")
    ETL_BATCH_SIZE: int       = Field(50, description="SP-API page size (max 100)")
    ORDERS_LOOKBACK_DAYS: int = Field(7, description="Default order lookback window (days)")


settings = Settings()  # type: ignore[call-arg]
