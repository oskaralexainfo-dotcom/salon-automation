from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from anthropic import Anthropic
from dotenv import load_dotenv
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os
import json

# Загружаем переменные из .env файла (включая наш API-ключ)
load_dotenv()

# Создаём клиента для общения с Claude
client = Anthropic()

# Создаём наш веб-сервер
app = FastAPI()


# --- ПАМЯТЬ ДИАЛОГА ---
# Простое хранилище в оперативной памяти: ключ — session_id (условный "номер разговора"),
# значение — список всех сообщений в этом разговоре.
# ВАЖНО: это хранилище очищается при перезапуске сервера — для демо этого достаточно,
# для реального продакшена позже заменим на настоящую базу данных.
conversations: dict[str, list] = {}


# --- НАСТРОЙКА GOOGLE CALENDAR ---

GOOGLE_CREDENTIALS_FILE = "google-credentials.json"
CALENDAR_ID = "oskaralexa.info@gmail.com"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# На своём компьютере мы читаем ключ из файла google-credentials.json.
# На Railway (и любом другом хостинге) этого файла не будет — там мы передадим
# то же самое содержимое через переменную окружения GOOGLE_CREDENTIALS_JSON.
google_credentials_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# ВРЕМЕННАЯ ДИАГНОСТИКА — покажет в логах, видит ли Railway переменную вообще.
# Уберём эту строку, как только разберёмся с проблемой.
print(f"DEBUG: GOOGLE_CREDENTIALS_JSON длина = {len(google_credentials_env) if google_credentials_env else 0}")

if google_credentials_env:
    # Мы на хостинге — достаём ключ из переменной окружения
    credentials_info = json.loads(google_credentials_env)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=SCOPES
    )
else:
    # Мы на своём компьютере — читаем ключ из обычного файла
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )

calendar_service = build("calendar", "v3", credentials=credentials)


def create_booking(service_name: str, date: str, time: str, duration_minutes: int = 60):
    start_datetime_str = f"{date}T{time}:00"
    start_dt = datetime.fromisoformat(start_datetime_str)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event = {
        "summary": f"Fair Eriu — {service_name}",
        "description": "Booking created via the Fair Eriu AI assistant",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Dublin"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Dublin"},
    }

    created_event = calendar_service.events().insert(
        calendarId=CALENDAR_ID, body=event
    ).execute()

    return created_event.get("htmlLink")


TOOLS = [
    {
        "name": "create_booking",
        "description": (
            "Creates a client booking in the salon's calendar. "
            "Use this function ONLY once the client has explicitly confirmed "
            "the service, date, and time of the appointment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Name of the service"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "string", "description": "Time in HH:MM format"},
                "duration_minutes": {"type": "integer", "description": "Approximate duration in minutes (default 60)"}
            },
            "required": ["service_name", "date", "time"]
        }
    }
]


# Теперь сообщение от клиента содержит ещё и session_id —
# условный "номер разговора", чтобы мы знали, к какой истории его добавлять
class ChatMessage(BaseModel):
    message: str
    session_id: str = "default"  # если фронтенд пока не присылает id — используем общий


SYSTEM_PROMPT = f"""You are a friendly AI assistant for "Fair Eriu", a beauty salon in Dublin, Ireland.

YOUR ROLE:
Answer clients' questions about services, prices, opening hours, and help them book appointments.
Do NOT mention that you are Claude or an Anthropic product — to the client, you are simply the salon's assistant.

TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}

OPENING HOURS:
Monday–Friday: 9:00–19:00
Saturday: 9:00–17:00
Sunday: closed

SERVICES AND PRICES:

Nails:
- Classic manicure — €35
- Gel polish (Shellac) — €45
- Nail extensions (gel) — €60
- Sensitive nail manicure (hypoallergenic products, acetone-free) — €40
- Classic pedicure — €45
- Gel polish pedicure — €55

Lashes & Brows:
- Classic lash extensions — €80
- Volume lash extensions (2D/3D) — €100
- Lash lamination — €55
- Brow lamination — €45
- Brow shaping & tinting — €20
- Microblading — €250

Hair:
- Women's haircut — from €45
- Men's haircut — from €25
- Full colour — from €90
- Balayage/highlights — from €120
- Event styling — from €60
- Ammonia-free colour (for sensitive scalps) — from €100

Facials:
- Classic facial cleanse — €65
- Chemical peel — €80
- Pregnancy-safe facial — €60
- Sensitive/rosacea-prone skin treatment — €70

Hair Removal:
- Sugaring (bikini area) — €35
- Full leg wax — €40
- Sugaring (gentler alternative for sensitive skin) — from €30

IMPORTANT RULES:
1. Before any colouring service (hair, brows, lashes), EU law requires a patch test at least 48 hours before the appointment. Always mention this to the client when they ask about colouring.
2. We offer cruelty-free and vegan products on request — mention this if the client asks.
3. The salon has accessible entry for people with limited mobility.
4. If a question is not about the salon's services, politely bring the conversation back to the salon.
5. Keep answers short, friendly, and to the point. Use a natural, conversational tone.
6. You REMEMBER previous messages in this conversation — use that context. If the client says "book me for that" or "how much would it cost" — refer back to whatever service was discussed earlier in this same conversation.

BOOKING APPOINTMENTS:
When a client wants to book — confirm the service, date, and desired time (use today's date as a reference if the client says "tomorrow", "on Friday", etc.). Make sure the time falls within opening hours. Only after the client has EXPLICITLY confirmed all the details — call the create_booking function.
"""


@app.get("/")
def read_root():
    return FileResponse("index.html")


@app.get("/status")
def status_check():
    return {"status": "Сервер работает!"}


@app.post("/chat")
def chat(user_message: ChatMessage):
    session_id = user_message.session_id

    # Если это первое сообщение в этом разговоре — создаём для него пустую историю
    if session_id not in conversations:
        conversations[session_id] = []

    # Достаём историю именно ЭТОГО разговора
    history = conversations[session_id]

    # Добавляем новое сообщение клиента в историю
    history.append({"role": "user", "content": user_message.message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=history  # теперь отправляем ВСЮ историю, а не одно сообщение
    )

    if response.stop_reason == "tool_use":
        tool_use_block = next(
            block for block in response.content if block.type == "tool_use"
        )

        booking_link = create_booking(
            service_name=tool_use_block.input["service_name"],
            date=tool_use_block.input["date"],
            time=tool_use_block.input["time"],
            duration_minutes=tool_use_block.input.get("duration_minutes", 60)
        )

        # Сохраняем в историю и запрос на инструмент, и результат его выполнения
        history.append({"role": "assistant", "content": response.content})
        history.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_block.id,
                    "content": f"Запись успешно создана. Ссылка на событие: {booking_link}"
                }
            ]
        })

        final_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history
        )

        reply_text = final_response.content[0].text
        # Сохраняем финальный ответ ассистента в историю
        history.append({"role": "assistant", "content": final_response.content})
        return {"reply": reply_text}

    # Обычный ответ без записи в календарь — тоже сохраняем в историю
    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": response.content})
    return {"reply": reply_text}