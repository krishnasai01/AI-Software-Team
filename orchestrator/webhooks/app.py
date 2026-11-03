"""GitHub webhook receiver. Verify, store (de-duplicated), reply immediately, then route.

If routing fails the event stays unprocessed in `events_inbox` and the reconciler replays it.
Run: uvicorn orchestrator.webhooks.app:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from temporalio.client import Client

from core.db import close_pool, get_pool
from core.settings import get_settings
from orchestrator import state
from orchestrator.webhooks.router import route

log = logging.getLogger(__name__)
HANDLED_EVENTS = {"issues", "pull_request"}


def valid_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await get_pool()
    app.state.temporal = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def process(client: Client, delivery_id: str, event: str, payload: dict) -> None:
    try:
        outcome = await route(client, event, payload)
        log.info("delivery %s (%s.%s): %s", delivery_id, event, payload.get("action"), outcome)
        await state.mark_event(delivery_id)
    except Exception as e:  # noqa: BLE001 - left unprocessed for the reconciler
        log.exception("routing delivery %s failed", delivery_id)
        await state.mark_event(delivery_id, error=f"{type(e).__name__}: {e}")


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
) -> dict:
    body = await request.body()
    if not valid_signature(get_settings().github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad signature")
    if x_github_event not in HANDLED_EVENTS:
        return {"status": "ignored"}
    payload = json.loads(body)
    if not await state.store_event(x_github_delivery, x_github_event, payload):
        return {"status": "duplicate"}
    background.add_task(process, request.app.state.temporal, x_github_delivery, x_github_event, payload)
    return {"status": "accepted"}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
