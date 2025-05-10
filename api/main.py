import os

import httpx
from fastapi import (
    FastAPI,
    Depends,
    Request,
    BackgroundTasks,
    HTTPException,
    Query,
    Response,
)
from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from db import init_db
from deps import get_db, tenant_by_phone_id
from models import Message

app = FastAPI()
ai = AsyncOpenAI()  # READS OPENAI_API_KEY from environment

# — Startup: ensure tables exist —
@app.on_event("startup")
def on_startup():
    init_db()

# — Health-check for UptimeRobot etc. —
@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    return {"ok": True}

# — Webhook verification endpoint —
@app.get("/webhook", include_in_schema=False)
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_token == os.getenv("VERIFY_TOKEN"):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")

# — Main webhook handler —
@app.post("/webhook", include_in_schema=False)
async def webhook(
    req: Request,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
):
    payload = await req.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_id = value.get("metadata", {}).get("phone_number_id")
            tenant = tenant_by_phone_id(phone_id, db)

            for msg in value.get("messages", []):
                sender = msg.get("from")
                text = msg.get("text", {}).get("body", "")
                wa_msg_id = msg.get("id")

                # Save incoming message
                db.add(
                    Message(
                        tenant_id=tenant.id,
                        wa_msg_id=wa_msg_id,
                        role="user",
                        text=text,
                    )
                )
                db.commit()

                # Build chat history (last 10 messages)
                history = (
                    db.query(Message)
                      .filter_by(tenant_id=tenant.id)
                      .order_by(Message.id.desc())
                      .limit(10)
                      .all()[::-1]
                )
                chat = [
                    {"role": m.role, "content": m.text}
                    for m in history
                ]
                if not chat:
                    chat = [{"role": "system", "content": tenant.system_prompt}]

                # Launch AI reply in background
                bg.add_task(
                    handle_ai_reply,
                    tenant,
                    chat,
                    sender,
                    db,
                )

    return {"status": "received"}

# — Background task: call OpenAI and send WhatsApp reply —
async def handle_ai_reply(
    tenant,
    chat: list[dict],
    to: str,
    db: Session,
):
    resp = await ai.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=chat,
    )
    answer = resp.choices[0].message.content.strip()

    # Save assistant response
    db.add(
        Message(
            tenant_id=tenant.id,
            role="assistant",
            text=answer,
        )
    )
    db.commit()

    # Send via WhatsApp Cloud API
    await send_whatsapp(
        business_phone_id=tenant.phone_id,
        token=tenant.wh_token,
        to=to,
        text=answer,
    )

# — Helper to post messages to WhatsApp —
async def send_whatsapp(
    business_phone_id: str,
    token: str,
    to: str,
    text: str,
):
    url = f"https://graph.facebook.com/v19.0/{business_phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
