import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from app.database import init_db
from app.config import logger
import app.routes as web_routes
from app.routes import router as web_router
from app.api import router as api_router
from app.client_edit import install_client_edit_button_middleware, patch_disabled_clients_offline, router as client_edit_router
from app.dashboard_top import build_top_clients
from app.status_activity import patch_statuses_by_traffic
from app.web_gate import WebGateMiddleware

patch_disabled_clients_offline(web_routes)
patch_statuses_by_traffic(web_routes)
_original_dashboard_stats = web_routes.get_dashboard_stats

def dashboard_stats_with_summed_top_clients():
    stats = _original_dashboard_stats()
    stats["top_clients"] = build_top_clients(web_routes)
    return stats

web_routes.get_dashboard_stats = dashboard_stats_with_summed_top_clients

app = FastAPI(title="AmneziaWG Admin Panel MVP", description="AmneziaWG admin panel", version="1.0.0")
app.add_middleware(WebGateMiddleware)
install_client_edit_button_middleware(app)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting AmneziaWG web panel...")
    init_db()

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        root_path = request.scope.get("root_path", "").rstrip("/")
        if exc.detail == "Redirect to Login":
            return RedirectResponse(url=f"{root_path}/login", status_code=303)
        if exc.detail == "Redirect to Password Change":
            return RedirectResponse(url=f"{root_path}/settings/password", status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

static_path = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_path):
    os.makedirs(static_path, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_path), name="static")

app.include_router(api_router)
app.include_router(client_edit_router)
app.include_router(web_router)
