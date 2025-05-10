import logging
import os
from alembic.config import Config
from alembic import command
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
ai = AsyncOpenAI()  # читает OPENAI_API_KEY из окружения
    
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.on_event("startup")
def startup(): 
    # Явные принты — точно попадут в лог
    print(">>> STARTUP: running Alembic migrations")
    
    # Готовим конфиг Alembic
    here = os.path.dirname(__file__)
    cfg_path = os.path.join(here, "alembic.ini")
    alembic_cfg = Config(cfg_path)
    
    # Прогоняем все миграции до head
    command.upgrade(alembic_cfg, "head")
    
    print(">>> FINISHED: Alembic migrations complete")


# Простая проверка здоровья
@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    return {"ok": True}

# Верификация webhook-а от Meta/WhatsApp
@app.get("/webhook", include_in_schema=False)
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_token == os.getenv("VERIFY_TOKEN"):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")

# Основная точка входа для сообщений
@app.post("/webhook", include_in_schema=False)
async def webhook(
    req: Request,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
):
    payload = await req.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            meta = change.get("value", {}).get("metadata", {})
            phone_id = meta.get("phone_number_id")
            tenant = tenant_by_phone_id(phone_id, db)

            for msg in change.get("value", {}).get("messages", []):
                sender = msg.get("from")
                text = msg.get("text", {}).get("body", "")
                wa_msg_id = msg.get("id")

                # Сохраняем входящее сообщение
                db.add(
                    Message(
                        tenant_id=tenant.id,
                        wa_msg_id=wa_msg_id,
                        role="user",
                        text=text,
                    )
                )
                db.commit()

                # Собираем последние 10 сообщений для контекста
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

                # Асинхронно вызываем OpenAI и отправляем ответ в фоне
                bg.add_task(
                    handle_ai_reply,
                    tenant,
                    chat,
                    sender,
                    db,
                )

    return {"status": "received"}

# Фоновая задача: запрос к OpenAI + отправка в WhatsApp
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

    # Сохраняем ответ ассистента
    db.add(
        Message(
            tenant_id=tenant.id,
            role="assistant",
            text=answer,
        )
    )
    db.commit()

    # Отправляем через WhatsApp Cloud API
    await httpx.AsyncClient().post(
        f"https://graph.facebook.com/v19.0/{tenant.phone_id}/messages",
        headers={"Authorization": f"Bearer {tenant.wh_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": answer},
        },
    )
