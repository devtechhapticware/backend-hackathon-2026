"""
FAQ Matcher — Student Task #11
==============================
Hardest support-domain task.

Why it's hard:
  - Startup seeding: FAQs must exist in the DB before any match can work.
    If already seeded, skip (idempotent).
  - Dynamic context: ALL stored FAQs are fetched and formatted into the
    LLM prompt on every request. As the FAQ list grows so does the prompt.
  - Semantic matching: the LLM must judge similarity, not just keyword
    overlap — "I forgot my login" should match "How do I reset my password?".
  - Structured output: LLM must return {"matched": bool, "answer": str|null}.
    Malformed JSON or missing fields must be handled gracefully.
  - Fallback logic: if matched=false the answer is overridden to
    "Flagged for human review" and the query is logged as unmatched.
  - Two tables with different schemas that must both be written on every call.

Endpoints:
  POST /faq/match    — match a user question against the FAQ knowledge base
  GET  /faqs         — list all stored FAQs
  POST /faqs         — add a new FAQ entry (admin use)
  GET  /faq/queries  — list all past query logs

Startup:
  Tables are created and FAQs are seeded automatically on first run.
"""

import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import llm
from database import Base, SessionLocal, engine, get_db
from models import FAQ, FAQQuery

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger("faq_matcher")

# ---------------------------------------------------------------------------
# Seed data — added once at startup, skipped if already present
# ---------------------------------------------------------------------------

SEED_FAQS = [
    {
        "question": "How do I reset my password?",
        "answer": "Go to the login page and click 'Forgot Password'. Enter your email and follow the reset link.",
    },
    {
        "question": "How do I cancel my subscription?",
        "answer": "Go to Settings > Billing > Cancel Plan. Your access continues until the end of the billing period.",
    },
    {
        "question": "How do I update my payment method?",
        "answer": "Go to Settings > Billing > Payment Methods and click 'Add Card'.",
    },
    {
        "question": "Can I export my data?",
        "answer": "Yes. Go to Settings > Data > Export. You will receive a download link by email within 24 hours.",
    },
    {
        "question": "How do I contact support?",
        "answer": "Email support@exoq.ai or use the in-app chat widget (bottom-right corner).",
    },
]


def seed_faqs(db: Session) -> None:
    """Insert seed FAQs only if the table is empty — safe to call on every restart."""
    if db.query(FAQ).count() > 0:
        logger.info("FAQs already seeded — skipping.")
        return
    for entry in SEED_FAQS:
        db.add(FAQ(question=entry["question"], answer=entry["answer"]))
    db.commit()
    logger.info("Seeded %d FAQs.", len(SEED_FAQS))


# ---------------------------------------------------------------------------
# App + lifecycle
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FAQ Matcher",
    description="Semantic FAQ matching powered by an LLM. Student Task #11.",
    version="1.0.0",
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_faqs(db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FAQMatchRequest(BaseModel):
    user_question: str = Field(..., min_length=3, description="The user's question to match against the FAQ knowledge base.")


class FAQMatchResponse(BaseModel):
    matched: bool
    answer: str
    user_question: str


class FAQCreate(BaseModel):
    question: str = Field(..., min_length=5)
    answer:   str = Field(..., min_length=5)


class FAQResponse(BaseModel):
    id:       int
    question: str
    answer:   str

    class Config:
        from_attributes = True


class FAQQueryResponse(BaseModel):
    id:         int
    question:   str
    matched:    bool
    answer:     str
    created_at: str

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    "/faq/match",
    response_model=FAQMatchResponse,
    summary="Match a user question against the stored FAQ knowledge base",
)
def match_faq(body: FAQMatchRequest, db: Session = Depends(get_db)):
    """
    Core endpoint — the hardest part of this task.

    Flow:
      1. Fetch all FAQs from DB and format them into a prompt context.
      2. Ask the LLM: does any FAQ answer this question? If so, which answer?
      3. Parse the LLM's JSON response.
      4. If not matched, override answer to "Flagged for human review".
      5. Store the query + result in faq_queries for audit.
      6. Return the result.

    The LLM does semantic similarity — "I forgot my login" correctly matches
    "How do I reset my password?" even with no shared keywords.
    """
    faqs = db.query(FAQ).all()
    if not faqs:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FAQ knowledge base is empty. Seed FAQs first.",
        )

    # Build the context from every stored FAQ
    faq_context = "\n".join(
        f"Q: {f.question}\nA: {f.answer}" for f in faqs
    )

    prompt = (
        f"You are a support assistant. Given the following FAQs:\n\n"
        f"{faq_context}\n\n"
        f"Match this user question: \"{body.user_question}\"\n\n"
        f"If one of the FAQs answers it (even partially or via paraphrase), "
        f"return the answer from that FAQ.\n"
        f"If none of the FAQs are relevant, set matched to false.\n"
        f'Reply as JSON only: {{"matched": true/false, "answer": "the FAQ answer" or null}}'
    )

    try:
        raw = llm.chat(prompt)
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM service error: {e}")

    # Parse — strip markdown fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")[1:]
        cleaned = "\n".join(lines[:-1] if lines[-1].strip() == "```" else lines).strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("LLM returned non-JSON: %s", raw[:200])
        raise HTTPException(status_code=502, detail="LLM returned malformed JSON.")

    matched: bool = bool(result.get("matched", False))
    answer: str   = result.get("answer") or "Flagged for human review"

    if not matched:
        answer = "Flagged for human review"

    # Persist the query log
    record = FAQQuery(question=body.user_question, matched=matched, answer=answer)
    db.add(record)
    db.commit()

    logger.info("FAQ match: matched=%s question='%s'", matched, body.user_question[:60])
    return FAQMatchResponse(matched=matched, answer=answer, user_question=body.user_question)


@app.get("/faqs", response_model=list[FAQResponse], summary="List all stored FAQs")
def list_faqs(db: Session = Depends(get_db)):
    return db.query(FAQ).all()


@app.post("/faqs", response_model=FAQResponse, status_code=201, summary="Add a new FAQ entry")
def add_faq(body: FAQCreate, db: Session = Depends(get_db)):
    faq = FAQ(question=body.question, answer=body.answer)
    db.add(faq)
    db.commit()
    db.refresh(faq)
    return faq


@app.get("/faq/queries", response_model=list[FAQQueryResponse], summary="List all past query logs")
def list_queries(db: Session = Depends(get_db)):
    return db.query(FAQQuery).order_by(FAQQuery.created_at.desc()).all()


@app.get("/health", summary="Liveness check")
def health():
    return {"status": "online", "agent": "faq_matcher"}
