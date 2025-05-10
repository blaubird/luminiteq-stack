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

from .db import init_db
from .deps import get_db, tenant_by_phone_id
from .models import Message

app = FastAPI()
ai = AsyncOpenAI()  # использует OPENAI_API_KEY из окружения

# При старте создаём таблицы, если их ещё нет
@app.on_event("startup")
def on_startup():
    init_db()


# Здоровье сервиса (для UptimeRobot и локального теста)
@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    return {"ok": True}


# Верификация Webhook-а от Meta/WhatsApp
@app.get("/webhook", include_in_schema=False)
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
        # Meta ожидает plain-text ответ
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")


# Обработка входящих сообщений
@app.post("/webhook", include_in_schema=False)
async def webhook(
    req: Request,
    bg: BackgroundTasks,
    db=Depends(get_db),
):
    payload = await req.json()

    # Проходим по всем записям и изменениям
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_id = value.get("metadata", {}).get("phone_number_id")
            messages = value.get("messages", [])

            # Обрабатываем каждый текстовый месседж
            for msg in messages:
                sender = msg.get("from")
                text = msg.get("text", {}).get("body", "")
                wa_msg_id = msg.get("id")

                # Определяем арендатора по номеру бизнес-телефона
                tenant = tenant_by_phone_id(phone_id, db)

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

                # Берём последние 10 сообщений этого тенанта
                history = (
                    db.query(Message)
                    .filter_by(tenant_id=tenant.id)
                    .order_by(Message.id.desc())
                    .limit(10)
                    .all()[::-1]
                )
                chat = [
                    {"role": m.role, "content": m.text} for m in history
                ] or [{"role": "system", "content": tenant.system_prompt}]

                # Запускаем AI-обработку и отправку в фоне
                bg.add_task(handle_ai_reply, tenant, chat, sender, db)

    return {"status": "received"}


async def handle_ai_reply(tenant, chat, to: str, db):
    # Вызываем OpenAI
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

    # Отправляем ответ в WhatsApp
    await send_whatsapp(
        business_phone_id=tenant.phone_id,
        token=tenant.wh_token,
        to=to,
        text=answer,
    )


async def send_whatsapp(
    business_phone_id: str, token: str, to: str, text: str
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
