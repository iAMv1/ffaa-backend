from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import invoices, clients, bank, tally, duplicates, reminders

app = FastAPI(title="FFAA - Free Accounting Automation", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clients.router, prefix="/api/v1")
app.include_router(invoices.router, prefix="/api/v1")
app.include_router(bank.router, prefix="/api/v1")
app.include_router(tally.router, prefix="/api/v1")
app.include_router(duplicates.router, prefix="/api/v1")
app.include_router(reminders.router, prefix="/api/v1")

@app.get("/health")
def health_check():
    return {"status": "ok"}
