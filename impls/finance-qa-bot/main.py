"""
Financial Q&A Bot — Student Task #16
======================================
Hardest finance-domain task.

Why it's hard:
  - Three-layer dependency: the Q&A bot is useless without expenses and
    invoices already in the DB. Students must implement and test all three
    POST endpoints before /finance/ask returns meaningful answers.
  - Cross-table retrieval: two separate models (Expense + Invoice) are queried
    and their data is merged into a single LLM context window.
  - RAG-lite pattern: data is retrieved from DB, serialised into a prompt,
    and the LLM is instructed to answer ONLY from that data — not from its
    training knowledge. Unconstrained LLMs hallucinate financial figures.
  - Structured JSON extraction for invoices: the LLM must parse unstructured
    text into a schema with a nested line_items array. Markdown fences and
    minor formatting deviations are common and must be handled.
  - Context window discipline: too many records → slow/expensive; too few →
    incomplete answers. The 50/20 limits are a deliberate design choice.

Endpoints:
  POST /expenses          — submit expense; LLM auto-categorises
  GET  /expenses/summary  — total spend grouped by category
  POST /invoices          — submit raw invoice text; LLM extracts fields
  POST /finance/ask       — answer a natural-language finance question
  GET  /finance/history   — audit log of all Q&A pairs
"""

import json
import logging

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

import llm
from database import Base, engine, get_db
from models import Expense, FinanceQA, Invoice

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger("finance_qa")

EXPENSE_CATEGORIES = ["Travel", "Software", "Office", "Meals", "Other"]
EXPENSE_CONTEXT_LIMIT = 50
INVOICE_CONTEXT_LIMIT = 20

app = FastAPI(
    title="Financial Q&A Bot",
    description="RAG-lite finance assistant powered by an LLM. Student Task #16.",
    version="1.0.0",
)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExpenseCreate(BaseModel):
    employee_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    amount:      float = Field(..., gt=0)


class ExpenseResponse(BaseModel):
    id:          int
    employee_id: str
    description: str
    amount:      float
    category:    str
    created_at:  str

    class Config:
        from_attributes = True


class InvoiceCreate(BaseModel):
    raw_text: str = Field(..., min_length=10, description="Raw invoice text for LLM extraction.")


class InvoiceResponse(BaseModel):
    id:         int
    vendor:     str
    amount:     float
    due_date:   str | None
    line_items: list[dict]
    created_at: str

    class Config:
        from_attributes = True


class FinanceQuestionRequest(BaseModel):
    question: str = Field(..., min_length=5)


class FinanceQuestionResponse(BaseModel):
    question:     str
    answer:       str
    context_used: dict | None = None


# ---------------------------------------------------------------------------
# Expense endpoints
# ---------------------------------------------------------------------------

@app.post("/expenses", response_model=ExpenseResponse, status_code=201,
          summary="Submit an expense — LLM auto-categorises it")
def create_expense(body: ExpenseCreate, db: Session = Depends(get_db)):
    try:
        prompt = (
            f"Categorize this expense into exactly one of {EXPENSE_CATEGORIES}. "
            f"Reply with the category name only.\nExpense: {body.description}"
        )
        category = llm.chat(prompt).strip()
        if category not in EXPENSE_CATEGORIES:
            category = "Other"
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    expense = Expense(
        employee_id=body.employee_id,
        description=body.description,
        amount=body.amount,
        category=category,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    logger.info("Expense stored: id=%d category=%s amount=%.2f", expense.id, category, body.amount)
    return _expense_to_dict(expense)


@app.get("/expenses/summary", summary="Total spend grouped by category")
def expense_summary(db: Session = Depends(get_db)):
    rows = (
        db.query(Expense.category, func.sum(Expense.amount), func.count(Expense.id))
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )
    return [{"category": r[0], "total": float(r[1]), "count": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Invoice endpoints
# ---------------------------------------------------------------------------

@app.post("/invoices", response_model=InvoiceResponse, status_code=201,
          summary="Submit raw invoice text — LLM extracts vendor, amount, due date, line items")
def create_invoice(body: InvoiceCreate, db: Session = Depends(get_db)):
    prompt = (
        "Extract details from this invoice text. Reply as JSON only:\n"
        '{"vendor": "", "amount": 0.0, "due_date": "YYYY-MM-DD", '
        '"line_items": [{"description": "", "amount": 0.0}]}\n\n'
        f"Invoice text:\n{body.raw_text}"
    )

    try:
        raw = llm.chat(prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    data = _parse_json_with_retry(raw)
    if data is None:
        raise HTTPException(status_code=502, detail="LLM returned malformed JSON. Rephrase the invoice text.")

    invoice = Invoice(
        vendor=str(data.get("vendor") or "Unknown"),
        amount=float(data.get("amount") or 0.0),
        due_date=str(data.get("due_date") or ""),
        raw_text=body.raw_text,
        line_items=json.dumps(data.get("line_items") or []),
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    logger.info("Invoice stored: id=%d vendor=%s amount=%.2f", invoice.id, invoice.vendor, invoice.amount)
    return _invoice_to_dict(invoice)


# ---------------------------------------------------------------------------
# Finance Q&A — core endpoint
# ---------------------------------------------------------------------------

@app.post("/finance/ask", response_model=FinanceQuestionResponse,
          summary="Ask a natural-language question about your expenses and invoices")
def finance_qa(body: FinanceQuestionRequest, db: Session = Depends(get_db)):
    """
    RAG-lite pattern:
      1. Retrieve recent expenses and invoices from DB.
      2. Serialise into a compact JSON context.
      3. Inject into a grounded LLM prompt.
      4. Store Q&A pair.
      5. Return answer.
    """
    # Step 1: Retrieve context
    expenses = (
        db.query(Expense)
        .order_by(Expense.created_at.desc())
        .limit(EXPENSE_CONTEXT_LIMIT)
        .all()
    )
    invoices = (
        db.query(Invoice)
        .order_by(Invoice.created_at.desc())
        .limit(INVOICE_CONTEXT_LIMIT)
        .all()
    )

    # Step 2: Serialise
    expense_ctx = [
        {"emp": e.employee_id, "desc": e.description[:80], "amt": float(e.amount), "cat": e.category}
        for e in expenses
    ]
    invoice_ctx = [
        {"vendor": i.vendor, "amt": float(i.amount), "due": i.due_date}
        for i in invoices
    ]

    # Step 3: Grounded prompt
    prompt = (
        "You are a finance assistant. Answer ONLY using the data below. "
        "Do not invent numbers. If the data is insufficient, say so.\n\n"
        f"Expenses ({len(expense_ctx)} records):\n{json.dumps(expense_ctx, indent=2)}\n\n"
        f"Invoices ({len(invoice_ctx)} records):\n{json.dumps(invoice_ctx, indent=2)}\n\n"
        f"Question: {body.question}"
    )

    try:
        answer = llm.chat(prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    # Step 4: Persist Q&A (non-fatal)
    try:
        record = FinanceQA(question=body.question, answer=answer)
        db.add(record)
        db.commit()
    except Exception as e:
        logger.warning("Failed to persist Q&A record: %s", e)
        db.rollback()

    logger.info("Finance Q&A answered. context=expenses:%d invoices:%d", len(expenses), len(invoices))
    return FinanceQuestionResponse(
        question=body.question,
        answer=answer,
        context_used={"expenses": len(expenses), "invoices": len(invoices)},
    )


@app.get("/finance/history", summary="All stored Q&A pairs, newest first")
def finance_history(db: Session = Depends(get_db)):
    records = db.query(FinanceQA).order_by(FinanceQA.created_at.desc()).all()
    return [{"id": r.id, "question": r.question, "answer": r.answer} for r in records]


@app.get("/health", summary="Liveness check")
def health():
    return {"status": "online", "agent": "finance_qa_bot"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_with_retry(raw: str) -> dict | None:
    """Try to parse JSON; on failure strip markdown fences and retry once."""
    for text in (raw, _strip_fences(raw)):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return s


def _expense_to_dict(e: Expense) -> dict:
    return {
        "id": e.id, "employee_id": e.employee_id, "description": e.description,
        "amount": float(e.amount), "category": e.category,
        "created_at": e.created_at.isoformat(),
    }


def _invoice_to_dict(i: Invoice) -> dict:
    return {
        "id": i.id, "vendor": i.vendor, "amount": float(i.amount),
        "due_date": i.due_date, "line_items": json.loads(i.line_items),
        "created_at": i.created_at.isoformat(),
    }
