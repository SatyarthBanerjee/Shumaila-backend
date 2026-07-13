from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- Existing status models (kept) ----------
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str


# ---------- Contact form models ----------
class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    type: Optional[str] = Field(default="Enquiry", max_length=40)
    message: str = Field(min_length=1, max_length=5000)


# ---------- SMTP helper ----------
def _send_email_sync(subject: str, body_text: str, body_html: str, reply_to: str) -> Optional[str]:
    """
    Send an email using SMTP credentials from env.
    Required env vars for delivery:
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    Optional:
        SMTP_FROM (defaults to SMTP_USER)
        SMTP_TO   (defaults to CONTACT_TO or arzooshumaila0@gmail.com)
    Returns None on success, or an error string on failure/misconfig.
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("SMTP_FROM") or user
    recipient = os.environ.get("SMTP_TO") or os.environ.get("CONTACT_TO") or "arzooshumaila0@gmail.com"

    if not (host and user and password):
        return "smtp_not_configured"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Shumaila Arzoo Studio", sender))
    msg["To"] = recipient
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(user, password)
                server.sendmail(sender, [recipient], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.sendmail(sender, [recipient], msg.as_string())
        return None
    except Exception as e:
        logger.exception("SMTP send failed")
        return f"smtp_error: {type(e).__name__}"


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


@api_router.post("/contact")
async def create_contact(payload: ContactCreate):
    """Store the enquiry and attempt to email it. Always saves to DB; email is best-effort."""
    enquiry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    doc = {
        "id": enquiry_id,
        "name": payload.name.strip(),
        "email": str(payload.email),
        "type": (payload.type or "Enquiry").strip(),
        "message": payload.message.strip(),
        "created_at": now.isoformat(),
        "email_status": "pending",
    }

    # Save to DB first — never lose a message
    try:
        await db.enquiries.insert_one(doc)
    except Exception as e:
        logger.exception("Failed to save enquiry")
        raise HTTPException(status_code=500, detail="Could not save your enquiry. Please try again shortly.")

    # Compose email content
    subject = f"New enquiry from portfolio — {doc['name']} ({doc['type']})"
    body_text = (
        f"New enquiry from the portfolio website\n"
        f"---------------------------------------\n"
        f"Name:    {doc['name']}\n"
        f"Email:   {doc['email']}\n"
        f"Type:    {doc['type']}\n"
        f"When:    {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Enq ID:  {enquiry_id}\n\n"
        f"Message:\n{doc['message']}\n"
    )
    body_html = f"""
      <div style="font-family:Inter,Arial,sans-serif;color:#263317;">
        <h2 style="font-family:Georgia,serif;font-style:italic;color:#4f1e14;margin:0 0 12px;">New enquiry — portfolio</h2>
        <table style="border-collapse:collapse;">
          <tr><td style="padding:4px 12px 4px 0;color:#6E7247;">Name</td><td style="padding:4px 0;"><b>{doc['name']}</b></td></tr>
          <tr><td style="padding:4px 12px 4px 0;color:#6E7247;">Email</td><td style="padding:4px 0;"><a href="mailto:{doc['email']}" style="color:#4f1e14;">{doc['email']}</a></td></tr>
          <tr><td style="padding:4px 12px 4px 0;color:#6E7247;">Type</td><td style="padding:4px 0;">{doc['type']}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;color:#6E7247;">When</td><td style="padding:4px 0;">{now.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;color:#6E7247;">ID</td><td style="padding:4px 0;font-family:monospace;font-size:12px;">{enquiry_id}</td></tr>
        </table>
        <div style="margin-top:20px;padding:16px;border-left:3px solid #a7ad55;background:#F2EFDF;white-space:pre-wrap;font-size:15px;line-height:1.55;">
{doc['message']}
        </div>
      </div>
    """

    # Attempt email — non-blocking failure
    err = await asyncio.to_thread(_send_email_sync, subject, body_text, body_html, doc["email"])
    if err is None:
        await db.enquiries.update_one({"id": enquiry_id}, {"$set": {"email_status": "sent"}})
        return {"ok": True, "id": enquiry_id, "delivery": "email"}
    else:
        await db.enquiries.update_one({"id": enquiry_id}, {"$set": {"email_status": err}})
        # Still success from user POV — we saved it and will follow up manually.
        return {"ok": True, "id": enquiry_id, "delivery": "stored", "note": err}


@api_router.get("/contact/count")
async def contact_count():
    """Utility endpoint for the studio to check how many enquiries were received."""
    total = await db.enquiries.count_documents({})
    return {"total": total}


# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
