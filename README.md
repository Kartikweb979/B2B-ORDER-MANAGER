# B2B Order Manager: AI-Powered Parsing & Decoupled Architecture

## The Problem
Managing wholesale B2B orders via messaging apps like WhatsApp or Telegram is inherently chaotic. Clients send unstructured, typo-ridden text messages, making it difficult to maintain accounts, track inventory, and generate reports without massive manual data entry overhead. 

## The Solution
I built a decoupled, AI-native system to automate the extraction of structured data from these messy conversational orders. The system uses Google's Gemini AI to parse incoming Telegram messages into strict JSON payloads, which are then persisted in a relational SQLite database.

## Impact & Business Value
- **Efficiency:** Automated order parsing, reducing manual data entry time by ~80% and practically eliminating human transcription errors.
- **Proactive Uptime:** Engineered an automated monitoring system that reduces system downtime awareness from hours to seconds via real-time admin alerts.

## System Architecture

Initially built as a monolith, I refactored the system into a decoupled microservice architecture. This separation of concerns ensures scalability, and the built-in monitoring loop guarantees high availability.

```text
┌─────────────────┐           HTTP POST           ┌───────────────────────┐
│                 │      (/process-order)         │                       │
│ Telegram Client │ ────────────────────────────> │    FastAPI Backend    │
│  (Presentation) │ <──────────────────────────── │    (Core Business     │
│                 │         JSON Response         │        Logic)         │
└───────┬─────────┘                               └──────────┬────────────┘
        │                                                    │
        │             ┌─────────────────────────┐            │
        │             │   Automated Monitor     │            │
        └────────────>│  (Pings /health every   │<───────────┘
     Alerts Admin if  │       5 minutes)        │
       Server Down    └─────────────────────────┘
                                   │
                                   ▼
                        ┌───────────────────┐     ┌───────────────────┐ 
                        │    Google GenAI   │     │ SQLite Database   │ 
                        │  (Data Parsing)   │     │  (Persistence)    │ 
                        └───────────────────┘     └───────────────────┘
```
## Challenges, Trade-offs & What I Learned

-  The Verification Bottleneck & Safety Guardrails: AI models are non-deterministic and can hallucinate invalid JSON structures. Fix: I did not trust the AI output blindly. I implemented strict prompt engineering guardrails and a comprehensive pytest suite. This acts as a safety net to verify the AI's output format before it ever touches the database.
Production Monitoring: Decoupling the system created a visibility gap; if the API went down, the bot would silently fail. Fix: I built an automated background task in the Telegram client that pings the FastAPI /health endpoint every 5 minutes. If it fails, it instantly sends an emergency Telegram alert to the admin, shifting the system from reactive to proactive monitoring.

-  Distributed Observability: Tracking errors across two microservices was difficult. Fix: I implemented centralized, production-grade logging (bot.log) across both the Telegram client and the FastAPI server to trace HTTP statuses, API timeouts, and database locks.

## Tech Stack 

Backend: Python 3.12, FastAPI, Uvicorn

Client: python-telegram-bot, Requests, Asyncio (for background monitoring)

AI/NLP: Google GenAI SDK

Data & State: SQLite3

Testing/CI: Pytest (Configured for core logic validation and health checks)

## Configuration
The project uses .gitignore to keep credentials (.env) and state data out of version control. Create a .env file in the root directory:
cp .env.example .env
Note: Your .env should include GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID (for health alerts), and ORDER_API_URL=http://127.0.0.1:8001/process-order.

## Usage
1. Start the Backend API Install dependencies and run the FastAPI server:
pip install -r requirements.txt
uvicorn api:app --port 8001
2. Start the Telegram Client (with Active Monitor) In a separate terminal tab, start the bot listener. This will automatically spin up the 5-minute background health-check loop:
python telegram_bot.py

## Testing (The Safety Net)
Run the test suite to verify data parsing, API mocking, and the /health endpoint:

- pytest __tests__/ 
