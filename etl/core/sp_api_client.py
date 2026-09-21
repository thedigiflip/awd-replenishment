"""
SP-API per-marketplace client factory.

每個 marketplace 可能屬於不同 region（na / eu / fe），
需要分別建立對應 region 的 API client。

Usage:
    from core.sp_api_client import get_orders_api

    for marketplace_id in settings.SP_API_MARKETPLACE_IDS:
        api = get_orders_api(marketplace_id)
        resp = api.get_orders(MarketplaceIds=[marketplace_id], ...)
"""

from sp_api.api import Orders, Inventories, Finances, Reports, AmazonWarehousingAndDistribution, ProductFees
from sp_api.base import Marketplaces
import structlog

from core.config import settings

log = structlog.get_logger()

# Marketplace ID → Marketplaces enum 對照
_MP_ENUM: dict[str, Marketplaces] = {
    "ATVPDKIKX0DER": Marketplaces.US,
    "A2EUQ1WTGCTBG2": Marketplaces.CA,
    "A1AM78C64UM0Y8": Marketplaces.MX,
    "A1F83G8C2ARO7P": Marketplaces.UK,
    "A1PA6795UKMFR9": Marketplaces.DE,
    "A13V1IB3VIYZZH": Marketplaces.FR,
    "APJ6JRA9NG5V4":  Marketplaces.IT,
    "A1RKKUPIHCS9HS": Marketplaces.ES,
    "A1VC38T7YXB528": Marketplaces.JP,
    "A39IBJ37TRP1C6": Marketplaces.AU,
}


def _credentials(marketplace_id: str) -> dict:
    """
    每次呼叫都回傳新 dict，確保 sp_api library 不會重用過期的
    in-memory access token（LWA token 60 分鐘後過期）。

    依 marketplace 對應到正確 region 的 refresh_token：
      - NA (US/CA/MX) → SP_API_REFRESH_TOKEN
      - EU (UK/DE/...) → SP_API_REFRESH_TOKEN_EU（沒設 fallback NA）
      - FE (JP/AU/SG) → SP_API_REFRESH_TOKEN_FE（沒設 fallback NA）
    """
    creds: dict = {
        "refresh_token":     settings.refresh_token_for(marketplace_id),
        "lwa_app_id":        settings.SP_API_CLIENT_ID,
        "lwa_client_secret": settings.SP_API_CLIENT_SECRET,
    }
    if settings.AWS_ACCESS_KEY_ID:
        creds["aws_access_key"] = settings.AWS_ACCESS_KEY_ID
        creds["aws_secret_key"] = settings.AWS_SECRET_ACCESS_KEY
    if settings.AWS_ROLE_ARN:
        creds["role_arn"] = settings.AWS_ROLE_ARN
    return creds


def _make_api(cls, marketplace_id: str):
    """
    每次建立全新 API instance（不重用舊物件），
    強制 library 重新走 LWA 取得新 access token。
    """
    return cls(credentials=_credentials(marketplace_id), marketplace=_resolve(marketplace_id))


def _resolve(marketplace_id: str) -> Marketplaces:
    mp = _MP_ENUM.get(marketplace_id)
    if mp is None:
        log.warning("sp_api.unknown_marketplace", marketplace_id=marketplace_id, fallback="US")
        return Marketplaces.US
    return mp


def get_orders_api(marketplace_id: str) -> Orders:
    return _make_api(Orders, marketplace_id)


def get_inventory_api(marketplace_id: str) -> Inventories:
    return _make_api(Inventories, marketplace_id)


def get_finance_api(marketplace_id: str) -> Finances:
    return _make_api(Finances, marketplace_id)


def get_reports_api(marketplace_id: str) -> Reports:
    return _make_api(Reports, marketplace_id)


def get_awd_api(marketplace_id: str) -> AmazonWarehousingAndDistribution:
    return _make_api(AmazonWarehousingAndDistribution, marketplace_id)


def get_fees_api(marketplace_id: str) -> ProductFees:
    return _make_api(ProductFees, marketplace_id)
