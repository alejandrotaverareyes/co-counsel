# main.py
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
import json
import re

from plugins import (
    get_agents,
    get_agent_chat_prompt,
    get_section_analysis_system,
    get_email_draft_system,
    get_summary_system,
    get_addon_system,
    get_enhance_system,
    get_parse_system,
    parse_section_response,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = FastAPI(title="Draft Partner API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key:
    raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to co-counsel-backend/.env")
client = AsyncAnthropic(api_key=api_key)


# ── Request models ──────────────────────────────────────────────────────────

class ParseRequest(BaseModel):
    memo_text: str
    agent_id: str = "employment"


class AnalyzeRequest(BaseModel):
    section_text: str              # only section 0 paragraphs — never the full memo
    audience: str
    agent_id: str
    scope_paras: list[str] = []   # paragraph numbers the attorney has actually reached


class SectionRequest(BaseModel):
    section_label: str
    section_text: str       # plain-text paragraphs from the source document
    section_index: int
    confirmed_draft: str    # accumulated HTML of confirmed sections so far
    agent_id: str
    audience: str
    scope_paras: list[str] = []   # paragraph numbers in this section


class SummarizeRequest(BaseModel):
    section_label: str
    section_text: str
    agent_id: str
    audience: str


class AddonRequest(BaseModel):
    attorney_draft: str    # attorney's HTML draft
    confirmed_notes: str   # accumulated confirmed analysis for context
    agent_id: str
    audience: str


class EnhanceRequest(BaseModel):
    attorney_draft: str    # existing HTML/text the attorney wrote
    section_text: str      # source section paragraphs
    section_label: str
    agent_id: str
    audience: str
    mode: str              # "summarize" | "analysis"


class EmailRequest(BaseModel):
    confirmed_notes: str    # HTML of the confirmed analysis
    audience: str           # "Client" | "Junior lawyer" | "Senior attorney"
    agent_id: str


class ChatMessage(BaseModel):
    role: str               # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    agent_id: str
    audience: str
    current_draft: str
    history: list[ChatMessage] = []


# ── Shared helpers ──────────────────────────────────────────────────────────

def _handle_anthropic_error(e: Exception):
    err = str(e)
    if "authentication_error" in err or "invalid x-api-key" in err:
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid Anthropic API key. Open co-counsel-backend/.env and set "
                "ANTHROPIC_API_KEY to a valid key from console.anthropic.com."
            ),
        )
    if "not_found_error" in err and "model" in err:
        raise HTTPException(status_code=400, detail="Model not available. Check the model id in main.py.")
    raise HTTPException(status_code=500, detail=err)


async def _run_section_analysis(
    system: str, section_label: str, source_text: str, confirmed_draft: str
) -> dict:
    """Call Claude for one section and parse the structured response."""
    confirmed_context = (
        f"CONFIRMED ANALYSIS SO FAR:\n{confirmed_draft}"
        if confirmed_draft.strip()
        else "CONFIRMED ANALYSIS SO FAR:\n(none — this is the first section)"
    )
    user_content = (
        f"{confirmed_context}\n\n"
        f'NEW SECTION TO ANALYZE: "{section_label}"\n'
        f"Source text:\n{source_text}"
    )
    response = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return parse_section_response(response.content[0].text)


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/agents")
async def list_agents():
    """Return all available legal AI agents."""
    return {"agents": get_agents()}


def _extract_json(raw: str) -> dict:
    """Try multiple strategies to extract valid JSON from a model response."""
    # Strategy 1: strip markdown fences and parse directly
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract the outermost {...} block (handles leading/trailing prose)
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3: normalize common smart-quote issues and retry
    normalized = (
        cleaned
        .replace("‘", "'").replace("’", "'")   # curly apostrophes → straight
        .replace("“", '"').replace("”", '"')   # curly quotes → straight
        .replace("–", "-").replace("—", "-")   # en/em dashes → hyphens
    )
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        pass

    # Strategy 4: attempt to fix trailing commas before ] or }
    fixed = re.sub(r",\s*([}\]])", r"\1", normalized)
    return json.loads(fixed)   # let this raise if still broken


@app.post("/api/parse")
async def parse_memo(request: ParseRequest):
    """Split the pasted memo into logical sections and paragraphs."""
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=get_parse_system(),
            messages=[{"role": "user", "content": request.memo_text}],
        )
        raw = response.content[0].text
        parsed = _extract_json(raw)
        sections = parsed.get("sections", [])
        if not sections:
            raise HTTPException(status_code=422, detail="Model returned no sections. Try again.")
        return {"status": "success", "sections": sections}
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Parse response was not valid JSON: {e}. Try again or paste a shorter memo."
        )
    except HTTPException:
        raise
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/analyze")
async def analyze_memo(request: AnalyzeRequest):
    """Analyze the opening section of the pasted memo (section 0 only)."""
    try:
        system = get_section_analysis_system(
            request.agent_id, request.audience,
            scope_paras=request.scope_paras or None,
        )
        result = await _run_section_analysis(
            system,
            "Jurisdiction & Background",
            request.section_text,
            "",
        )
        return {"status": "success", **result}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/section")
async def analyze_section(request: SectionRequest):
    """
    Analyze one source section with confirmed context.
    Claude decides whether any confirmed sections need cross-section amendments.
    """
    try:
        system = get_section_analysis_system(
            request.agent_id, request.audience,
            scope_paras=request.scope_paras or None,
        )
        result = await _run_section_analysis(
            system,
            request.section_label,
            request.section_text,
            request.confirmed_draft,
        )
        return {"status": "success", **result}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/email")
async def draft_email(request: EmailRequest):
    """
    Draft an audience-specific communication (client letter, junior briefing,
    or senior memo) from the confirmed analysis notes.
    """
    try:
        system = get_email_draft_system(request.audience)
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    "Draft the communication based on these confirmed analysis notes:\n\n"
                    + request.confirmed_notes
                ),
            }],
        )
        return {"status": "success", "email_html": response.content[0].text}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/summarize")
async def summarize_section(request: SummarizeRequest):
    """Generate a brief 2-3 bullet summary of one section."""
    try:
        system = get_summary_system(request.agent_id, request.audience)
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f'Summarize this section — "{request.section_label}":\n\n'
                    + request.section_text
                ),
            }],
        )
        return {"status": "success", "summary_html": response.content[0].text}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/addon")
async def ai_addon(request: AddonRequest):
    """Suggest additions to the attorney's self-drafted text using <ins> tags."""
    try:
        system = get_addon_system(request.agent_id, request.audience)
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Confirmed analysis context:\n{request.confirmed_notes}\n\n"
                    f"Attorney's draft:\n{request.attorney_draft}\n\n"
                    "Add legal suggestions using <ins> tags only. "
                    "Return the complete updated HTML."
                ),
            }],
        )
        return {"status": "success", "addon_html": response.content[0].text}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/enhance")
async def enhance_draft(request: EnhanceRequest):
    """
    Enhance the attorney's existing draft with AI additions using <ins> tags.
    The attorney's text is preserved verbatim; AI content is wrapped in <ins>.
    """
    try:
        system = get_enhance_system(request.agent_id, request.audience, request.mode)
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Section: {request.section_label}\n\n"
                    f"Source text:\n{request.section_text}\n\n"
                    f"Attorney's existing draft (preserve exactly):\n{request.attorney_draft}\n\n"
                    "Enhance the draft with <ins> additions. Return the complete HTML."
                ),
            }],
        )
        return {"status": "success", "enhanced_html": response.content[0].text}
    except Exception as e:
        _handle_anthropic_error(e)


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Continue an interactive editing session with the selected agent."""
    try:
        system = get_agent_chat_prompt(
            request.agent_id, request.audience, request.current_draft
        )
        messages = [{"role": m.role, "content": m.content} for m in request.history]
        messages.append({"role": "user", "content": request.message})
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=system,
            messages=messages,
        )
        return {"status": "success", "reply": response.content[0].text}
    except Exception as e:
        _handle_anthropic_error(e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
