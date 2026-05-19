"""
SP-API ETL FastAPI Application
Entry point — mounts all routers and initialises shared resources.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import structlog
import os

from core.config import settings
from core.database import init_db, close_db
from core.db_meta import init_meta_db
from routers import orders, inventory, ads, finance
from routers import replenishment, sz_warehouse, awd_upload, product_catalog, sales, traffic

log = structlog.get_logger()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    log.info("etl.startup", duckdb_path=settings.DUCKDB_PATH)
    await init_db()
    init_meta_db()   # SQLite run log (separate from DuckDB)
    yield
    log.info("etl.shutdown")
    await close_db()


app = FastAPI(
    title="SP-API ETL Service",
    description="Amazon SP-API → DuckDB ETL pipeline + Replenishment Dashboard",
    version="0.2.0",
    lifespan=lifespan,
)

# ─── CORS（n8n 在同一個 Docker network 內呼叫）────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── ETL Routers ─────────────────────────────────────────────────────────────
app.include_router(orders.router,       prefix="/etl/orders",       tags=["ETL - Orders"])
app.include_router(inventory.router,    prefix="/etl/inventory",    tags=["ETL - Inventory"])
app.include_router(ads.router,          prefix="/etl/ads",          tags=["ETL - Ads"])
app.include_router(finance.router,      prefix="/etl/finance",      tags=["ETL - Finance"])
app.include_router(sales.router,        prefix="/etl/sales",        tags=["ETL - Sales"])
app.include_router(traffic.router,      prefix="/etl/traffic",      tags=["ETL - Traffic"])

# ─── Warehouse Upload Routers ────────────────────────────────────────────────
app.include_router(sz_warehouse.router,   prefix="/etl/sz",      tags=["Warehouse - SZ"])
app.include_router(awd_upload.router,    prefix="/etl/awd",     tags=["Warehouse - AWD"])
app.include_router(product_catalog.router, prefix="/etl/catalog", tags=["Catalog"])

# ─── Replenishment API ────────────────────────────────────────────────────────
app.include_router(replenishment.router, prefix="/replenishment",   tags=["Replenishment"])

# ─── Static files (Dashboard) ────────────────────────────────────────────────
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "version": app.version}


# ─── Dashboard shortcut ───────────────────────────────────────────────────────
@app.get("/dashboard", tags=["System"], include_in_schema=False)
async def dashboard():
    """Redirect to the replenishment dashboard HTML."""
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/", tags=["System"])
async def root():
    return {
        "service":   "sp-api-etl",
        "version":   "0.2.0",
        "dashboard": "/dashboard",
        "docs":      "/docs",
        "endpoints": {
            "etl":           ["POST /etl/orders/sync", "POST /etl/inventory/sync",
                               "POST /etl/ads/sync", "POST /etl/finance/sync"],
            "warehouse":     ["POST /etl/sz/upload", "POST /etl/awd/upload"],
            "replenishment": ["GET /replenishment", "GET /replenishment/export",
                               "POST /replenishment/sku-config"],
        },
    }
