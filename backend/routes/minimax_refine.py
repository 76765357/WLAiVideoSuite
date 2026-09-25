from fastapi import APIRouter, Depends
from typing import Any
from database import get_db
from routes.llm_helper import get_system_prompt, call_llm

router = APIRouter(prefix="/minimax-refine", tags=["Minimax Refine"])


@router.post("/refine")
async def refine_prompt(payload: dict, db: Any = Depends(get_db)):
    """Refine a raw prompt or scenario into a well-formatted 6-section MiniMax prompt."""
    system_prompt = await get_system_prompt(db, "Minimax Prompt Refine")
    if not system_prompt:
        return {"status": "error", "details": "No system prompt mapped for 'Minimax Prompt Refine'. Map one in Settings > App Settings."}

    user_content = payload.get('prompt', '').strip()
    if not user_content:
        return {"status": "error", "details": "No prompt text provided."}

    try:
        result = await call_llm(db, system_prompt, user_content)
        return {"status": "success", "prompt": result}
    except Exception as e:
        return {"status": "error", "details": str(e)}
