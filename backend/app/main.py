from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings

app = FastAPI(title="Ecommerce Order Support Assistant", version="0.0.1")
app.state.settings = Settings()
app.include_router(health_router)
