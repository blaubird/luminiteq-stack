import os
import logging
from fastapi import FastAPI, Depends, Request, BackgroundTasks, HTTPException, Query, Response
from logging.config import fileConfig
from alembic.config import Config
from alembic import command
import httpx
from openai import AsyncOpenAI
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from deps import get_db, tenant_by_phone_id
from models import Message

# — Очистка ENV-переменных от лишних пробелов/переносов строки —
if os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY").strip()
if os.getenv("WH_TOKEN"):
    os.environ["WH_TOKEN"] = os.getenv("WH_TOKEN").strip()
if os.getenv("WH_PHONE_ID"):
    os.environ["WH_PHONE_ID"] = os.getenv("WH_PHONE_ID").strip()

app = FastAPI()
# Явно передаём API-ключ (уже без \n)
ai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Логирование INFO в stdout
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.on_event("startup")
def startup():
    # === 1) Run Alembic migrations ===
    print(">>> STARTUP: running Alembic migrations")
    here = os.path.dirname(__file__)
    cfg_path = os.path.join(here, "alembic.ini")
    alembic_cfg = Config(cfg_path)
    fileConfig(alembic_cfg.config_file_name)
    command.upgrade(alembic_cfg, "head")
    print(">>> STARTUP: migrations complete")

    # === 2) TEMP: seed test tenant for WhatsApp sandbox ===
    # remove entire block after verification
    from db import SessionLocal
    from models import Tenant

    db = SessionLocal()
    phone = os.getenv("WH_PHONE_ID")  # уже очищен
    token = os.getenv("WH_TOKEN")     # уже очищен
    if phone and token:
        exists = db.query(Tenant).filter_by(phone_id=phone).first()
        if not exists:
            print(">>> STARTUP: seeding test tenant")
            db.add(Tenant(
                id="test-tenant",
                phone_id=phone,
                wh_token=token,
                system_prompt="You are a helpful assistant."
            ))
            db.commit()
            print(">>> STARTUP: test tenant seeded")
    db.close()
    # === END TEMP SEED ===

# --- Health check ---
@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    return {"ok": True}

# --- Webhook verification endpoint ---
@app.get("/webhook", include_in_schema=False)
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_token == os.getenv("VERIFY_TOKEN"):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")

# --- Immediate test reply for WhatsApp sandbox ---
@app.post("/webhook", include_in_schema=False)
async def webhook(
    req: Request,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
):
    payload = await req.json()
    latest_answer = None

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            meta = change.get("value", {}).get("metadata", {})
            phone_id = meta.get("phone_number_id")
            tenant = tenant_by_phone_id(phone_id, db)

            for msg in change.get("value", {}).get("messages", []):
                sender = msg.get("from")
                text = msg.get("text", {}).get("body", "")
                wa_msg_id = msg.get("id")

                # === DUPLICATE GUARD START ===
                existing = db.query(Message).filter_by(wa_msg_id=wa_msg_id).first()
                if existing:
                    print(f">>> SKIP: duplicate wa_msg_id={wa_msg_id}")
                    continue
                # === DUPLICATE GUARD END ===

                # Save incoming message
                try:
                    db.add(Message(
                        tenant_id=tenant.id,
                        wa_msg_id=wa_msg_id,
                        role="user",
                        text=text
                    ))
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    print(f">>> IGNORE IntegrityError on wa_msg_id={wa_msg_id}")
                    continue

                # Build chat history (last 10 messages)
                history = (
                    db.query(Message)
                      .filter_by(tenant_id=tenant.id)
                      .order_by(Message.id.desc())
                      .limit(10)
                      .all()[::-1]
                )
                chat = (
                    [{"role": m.role, "content": m.text} for m in history]
                    or [{"role": "system", "content": tenant.system_prompt}]
                )

                # === TEMP: immediate reply via GPT-4o ===
                try:
                    resp = await ai.chat.completions.create(
                        model="gpt-4o",
                        messages=chat,
                    )
                    latest_answer = resp.choices[0].message.content.strip()
                    print(">>> TEMP: generated answer:", latest_answer)

                    # clean token from tenant in case it has newline
                    wh_token = tenant.wh_token.strip()
                    send_resp = await httpx.AsyncClient().post(
                        f"https://graph.facebook.com/v19.0/{tenant.phone_id}/messages",
                        headers={
                            "Authorization": f"Bearer {wh_token}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "messaging_product": "whatsapp",
                            "to": sender,
                            "type": "text",
                            "text": {"body": latest_answer},
                        },
                    )
                    print(f">>> TEMP: WhatsApp API status {send_resp.status_code}")
                    print(">>> TEMP: WhatsApp API response:", send_resp.text)
                    send_resp.raise_for_status()
                except Exception as e:
                    print(">>> TEMP ERROR sending reply:", e)
                    raise
                # === END TEMP immediate reply ===

    return {"status": "received", "echo": latest_answer}

# --- Background task for production use ---
async def handle_ai_reply(
    tenant,
    chat: list[dict],
    to: str,
    db: Session,
):
    resp = await ai.chat.completions.create(
        model="gpt-4o",
        messages=chat,
    )
    answer = resp.choices[0].message.content.strip()

    db.add(Message(
        tenant_id=tenant.id,
        role="assistant",
        text=answer,
    ))
    db.commit()

    wh_token = tenant.wh_token.strip()
    await httpx.AsyncClient().post(
        f"https://graph.facebook.com/v19.0/{tenant.phone_id}/messages",
        headers={"Authorization": f"Bearer {wh_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": answer},
        },
    )
