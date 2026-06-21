                                                 { B2B Order Manager: AI-Powered Parsing & Decoupled Architecture }

[ The Problem  ]
Managing wholesale B2B orders via messaging apps like WhatsApp or Telegram is inherently chaotic. Clients send unstructured, typo-ridden text messages, making it difficult to maintain accounts, track inventory, and generate reports without massive manual data entry overhead. 

[ The Solution  ]
I built a decoupled, AI-native system to automate the extraction of structured data from these messy conversational orders. The system uses Google's Gemini AI to parse incoming Telegram messages into strict JSON payloads, which are then persisted in a relational SQLite database.

[ Impact & Business Value   ]
- **Efficiency:** Automated order parsing, reducing manual data entry time by ~80% and practically eliminating human transcription errors.
- **Reporting:** Built a `/export` pipeline that dynamically queries the database and generates business-ready CSV reports on demand.

[ System Architecture  ]

Initially built as a monolith, I refactored the system into a decoupled microservice architecture. This separation of concerns ensures scalability, making it easy to add a web dashboard or mobile app in the future without touching the AI/DB logic.

┌─────────────────┐           HTTP POST           ┌───────────────────────┐
│                 │      (/process-order)         │                       │
│ Telegram Client │ ────────────────────────────> │    FastAPI Backend    │
│  (Presentation) │ <──────────────────────────── │    (Core Business     │
│                 │         JSON Response         │        Logic)         │
└─────────────────┘                               └──────────┬────────────┘
                                                             │
                                   ┌─────────────────────────┼─────────────────────────┐
                                   │                         │                         │
                                   ▼                         ▼                         ▼
                        ┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
                        │    Google GenAI   │     │ SQLite Database   │     │ Distributed       │
                        │    (NLP Engine)   │     │ (Persistence)     │     │ Logging (bot.log) │
                        └───────────────────┘     └───────────────────┘     └───────────────────┘
[  Challenges, Trade-offs & What I Learned:  ]

*The LLM Reliability Problem: AI models are non-deterministic. Initially, Gemini would occasionally return invalid JSON or timeout. Trade-off/Fix: Instead of trusting the output blindly,    I implemented prompt guardrails, strict JSON parsing validation, and a retry mechanism.

*The Observability Gap: When I decoupled the system into two services, debugging became difficult. Fix: I implemented centralized, production-grade logging (bot.log) across both the          Telegram client and the FastAPI server to trace HTTP statuses, API timeouts, and database locks.

*Git Version Control for Data: I quickly learned that tracking live production data files on Git causes merge conflicts during deployments. Fix: Added .db and .jsonl to .gitignore and         utilized git stash workflows during production pulls.

Tech Stack
[ Backend ]: Python 3.x, FastAPI, Uvicorn
[ Client] : python-telegram-bot, Requests
[ AI/NLP] : Google GenAI SDK
[ Data & State] : SQLite3
[ Testing/CI] : Pytest (Configured for core logic validation)

[  Local Setup & Execution   ]

1. Environment Variables
Copy the example environment file and add your actual credentials:
[   cp .env.example .env   ]
Note: Your .env should include GEMINI_API_KEY and TELEGRAM_BOT_TOKEN.
2. Start the Backend API
Install dependencies and run the FastAPI server:
   pip install fastapi uvicorn requests python-dotenv google-genai   
   uvicorn api:app --reload    
(The API will run locally on http://127.0.0.1:8000)
3. Start the Telegram Client
In a separate terminal tab, start the bot listener:
   pip install python-telegram-bot  
   python telegram_bot.py    
[  Testing  ]
Run the test suite to verify data parsing and API mocking:
   pytest __tests__/  
