"""GitHub webhook receiver. Verify, store (de-duplicated), reply. Nothing else.

Webhooks are only nudges: the reconciler picks stored events up within seconds and re-reads the
real state from GitHub, so a lost, late or duplicated event never changes the outcome, only the
latency. This process needs nothing but Postgres.
Run: uvicorn orchestrator.webhooks.app:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from core.db import close_pool, get_pool
from core.settings import get_settings
from orchestrator import state

HANDLED_EVENTS = {
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "check_suite",
    "check_run",
    "status",
}


def valid_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
) -> dict:
    body = await request.body()
    if not valid_signature(get_settings().github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad signature")
    if x_github_event not in HANDLED_EVENTS:
        return {"status": "ignored"}
    stored = await state.store_event(x_github_delivery, x_github_event, json.loads(body))
    return {"status": "accepted" if stored else "duplicate"}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
