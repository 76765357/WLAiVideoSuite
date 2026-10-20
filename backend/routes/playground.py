from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel
from typing import Dict, Any, Optional
import os
import json
import time
import uuid
import asyncio
import httpx
from database import get_db
from paths import WORKFLOWS_DIR, GENERATED_DIR
from .playground_parser import PlaygroundParser

router = APIRouter(prefix="/playground", tags=["Playground"])

# In-memory task tracker: task_id -> (prompt_id, timestamp)
active_tasks: Dict[str, tuple] = {}

# Default TTL (seconds) — overridden by ComfyUISettings.taskTTL from DB
DEFAULT_TASK_TTL = 600  # 10 minutes

@router.get("/list")
def list_workflows():
    files = os.listdir(WORKFLOWS_DIR)
    workflow_files = sorted([f for f in files if f.endswith(".json")])
    return [
        {"id": f.replace(".json", ""), "name": f.replace(".json", "").replace("_", " ")}
        for f in workflow_files
    ]

class GenerateRequest(BaseModel):
    workflow_id: str
    inputs: Dict[str, Any]

@router.get("/parse/{filename}")
def parse_workflow(filename: str):
    parser = PlaygroundParser()
    result = parser.parse(filename)
    if not result.get("is_valid") and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

def _inject_inputs(workflow_data: dict, inputs: Dict[str, Any]) -> dict:
    """Inject inputs into workflow nodes. Returns modified workflow."""
    for role, value in inputs.items():
        if (
            (role.startswith("image") or role.startswith("audio"))
            and isinstance(value, list)
            and len(value) >= 2
        ):
            node_id = str(value[0])
            filename = str(value[1])
            if node_id in workflow_data:
                input_key = "image" if role.startswith("image") else "audio"
                workflow_data[node_id].setdefault("inputs", {})[input_key] = filename
            continue

        target_node_id = None
        for nid, ndata in workflow_data.items():
            title = ndata.get("_meta", {}).get("title", "")
            if f"(input:{role})" in title.lower():
                target_node_id = nid
                break

        if target_node_id and target_node_id in workflow_data:
            inputs_dict = workflow_data[target_node_id].setdefault("inputs", {})
            if "value" in inputs_dict:
                inputs_dict["value"] = value
            elif role == "prompt":
                inputs_dict["text"] = value
            else:
                inputs_dict[role] = value
    return workflow_data


def _prune_unused_inputs(workflow_data: dict, provided_node_ids: set) -> dict:
    """Remove unused (input:image)/(input:audio) nodes not in provided_node_ids,
    and clean up any references to them in other nodes' inputs."""
    to_remove = []
    for nid, ndata in workflow_data.items():
        if not isinstance(ndata, dict):
            continue
        title = ndata.get("_meta", {}).get("title", "").lower()
        if "(input:image)" in title or "(input:audio)" in title:
            if nid not in provided_node_ids:
                to_remove.append(nid)

    if not to_remove:
        return workflow_data

    removed_set = set(to_remove)
    for nid in to_remove:
        del workflow_data[nid]

    # Clean up references to removed nodes in all remaining nodes
    for nid, ndata in workflow_data.items():
        if not isinstance(ndata, dict):
            continue
        inputs = ndata.get("inputs", {})
        for key, val in list(inputs.items()):
            if isinstance(val, list) and len(val) >= 1 and val[0] in removed_set:
                del inputs[key]

    print(f"[Prune] removed {len(to_remove)} unused input node(s): {to_remove}")
    return workflow_data


def _short_text(text: str, limit: int = 220) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _format_comfy_rejection(body: str) -> str:
    """Turn a ComfyUI /prompt error body into a short, readable message."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _short_text(body)

    parts = []
    node_errors = data.get("node_errors") or {}
    if isinstance(node_errors, dict):
        for node_id, info in node_errors.items():
            if not isinstance(info, dict):
                continue
            class_type = info.get("class_type") or "node"
            for err in info.get("errors") or []:
                if not isinstance(err, dict):
                    continue
                extra = err.get("extra_info") or {}
                input_name = extra.get("input_name")
                received = extra.get("received_value")
                if input_name and received is not None:
                    parts.append(f"{class_type} ({node_id}): {input_name} '{received}' is not available")
                else:
                    parts.append(f"{class_type} ({node_id}): {err.get('details') or err.get('message') or 'invalid'}")

    if parts:
        return "ComfyUI rejected the prompt. " + _short_text(" | ".join(parts))

    err = data.get("error") or {}
    if isinstance(err, dict) and err.get("message"):
        return _short_text(f"ComfyUI rejected the prompt: {err.get('message')}")
    return _short_text(body)


def _comfy_failure_detail(entry: dict) -> Optional[str]:
    """Read a finished-but-failed ComfyUI history entry.

    ComfyUI stores the outcome in status.status_str and status.messages,
    not in status.status.
    """
    status_info = entry.get("status") or {}
    if not isinstance(status_info, dict):
        return None

    for msg in status_info.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or not msg:
            continue
        event = msg[0]
        payload = msg[1] if len(msg) > 1 and isinstance(msg[1], dict) else {}
        if event == "execution_error":
            text = payload.get("exception_message") or payload.get("exception_type") or "ComfyUI generation failed"
            node_type = payload.get("node_type") or ""
            node_id = payload.get("node_id") or ""
            where = " ".join(part for part in (str(node_type), str(node_id)) if part).strip()
            if where:
                return _short_text(f"{text} ({where})")
            return _short_text(str(text))
        if event == "execution_interrupted":
            return "ComfyUI generation was interrupted"

    status_str = status_info.get("status_str") or status_info.get("status")
    if status_str == "error":
        return _short_text(status_info.get("message") or "ComfyUI generation failed")
    return None


def _queue_prompt_ids(queue_data: dict) -> set:
    ids = set()
    for key in ("queue_running", "queue_pending"):
        for item in queue_data.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ids.add(str(item[1]))
    return ids


async def _cancel_comfy_prompts(db, prompt_ids: list) -> None:
    """Interrupt and drop only the prompts this playground still tracks."""
    if not prompt_ids:
        return
    comfyui = await db.comfyuisettings.find_first()
    if not comfyui:
        return
    comfy_http = f"http://{comfyui.ip}:{comfyui.port}"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            queue_res = await client.get(f"{comfy_http}/queue")
            queue_data = queue_res.json() if queue_res.status_code == 200 else {}
            if _queue_prompt_ids({"queue_running": queue_data.get("queue_running") or []}).intersection(prompt_ids):
                await client.post(f"{comfy_http}/interrupt")
            await client.post(f"{comfy_http}/queue", json={"delete": prompt_ids})
    except Exception as exc:
        print(f"[Cleanup] ComfyUI cancel failed: {exc}")

@router.post("/generate")
async def generate(request: GenerateRequest, db: Any = Depends(get_db)):
    """Submit a generation to the ComfyUI queue. Returns task_id immediately."""
    workflow_id = request.workflow_id
    inputs = request.inputs

    full_path = os.path.join(WORKFLOWS_DIR, f"{workflow_id}.json")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found.")

    with open(full_path, "r", encoding="utf-8") as f:
        workflow_data = json.load(f)

    workflow_data = _inject_inputs(workflow_data, inputs)

    # Prune unused image/audio input nodes
    provided_node_ids = set()
    for role, value in inputs.items():
        if (
            (role.startswith("image") or role.startswith("audio"))
            and isinstance(value, list)
            and len(value) >= 2
        ):
            provided_node_ids.add(str(value[0]))
    workflow_data = _prune_unused_inputs(workflow_data, provided_node_ids)

    print(f"\n--- Queued Workflow ({workflow_id}) ---")
    print(json.dumps(workflow_data, indent=2))
    print("-----------------------------------------------\n")

    comfyui = await db.comfyuisettings.find_first()
    if not comfyui:
        raise HTTPException(status_code=500, detail="ComfyUI settings not found in database.")

    comfy_ip = comfyui.ip
    comfy_port = comfyui.port
    comfy_http = f"http://{comfy_ip}:{comfy_port}"

    task_id = str(uuid.uuid4())

    timeout = httpx.Timeout(30.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            payload = {"prompt": workflow_data}
            queue_res = await client.post(f"{comfy_http}/prompt", json=payload)

            if queue_res.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=_format_comfy_rejection(queue_res.text),
                )

            prompt_id = queue_res.json()["prompt_id"]
            active_tasks[task_id] = (prompt_id, time.time())
            print(f"[Queue] task_id={task_id} prompt_id={prompt_id} (active: {len(active_tasks)})")
    except HTTPException:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"ComfyUI is unreachable at {comfy_http}. {exc.__class__.__name__}",
        )

    # Small delay to let ComfyUI register the prompt in history before we return
    await asyncio.sleep(0.5)

    return {"task_id": task_id, "prompt_id": prompt_id}

@router.get("/status/{task_id}")
async def check_status(task_id: str, db: Any = Depends(get_db)):
    """Check if a queued generation is complete. Returns image or pending status."""
    if task_id not in active_tasks:
        print(f"[Status] task_id={task_id} NOT FOUND in active_tasks ({len(active_tasks)} tasks)")
        raise HTTPException(status_code=404, detail="Task not found.")

    prompt_id, queued_at = active_tasks[task_id]

    comfyui = await db.comfyuisettings.find_first()
    if not comfyui:
        raise HTTPException(status_code=500, detail="ComfyUI settings not found.")

    comfy_http = f"http://{comfyui.ip}:{comfyui.port}"

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        try:
            history_res = await client.get(f"{comfy_http}/history/{prompt_id}")
        except Exception as exc:
            print(f"[Status] task_id={task_id} history check failed: {exc}")
            return {"status": "pending", "task_id": task_id}

        if history_res.status_code != 200:
            print(f"[Status] task_id={task_id} history HTTP {history_res.status_code}")
            return {"status": "pending", "task_id": task_id}

        history_data = history_res.json()
        entry = history_data.get(prompt_id, {})

        # A missing history entry means the prompt is still queued, or ComfyUI dropped it.
        if not entry:
            age = time.time() - queued_at
            if age >= 20:
                try:
                    queue_res = await client.get(f"{comfy_http}/queue")
                    queue_data = queue_res.json() if queue_res.status_code == 200 else {}
                    still_queued = prompt_id in _queue_prompt_ids(queue_data)
                except Exception as exc:
                    print(f"[Status] task_id={task_id} queue check failed: {exc}")
                    still_queued = True
                if not still_queued:
                    history_res = await client.get(f"{comfy_http}/history/{prompt_id}")
                    if history_res.status_code == 200:
                        entry = history_res.json().get(prompt_id, {})
                    if not entry:
                        active_tasks.pop(task_id, None)
                        detail = "Generation is no longer in the ComfyUI queue"
                        print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> ERROR: {detail}")
                        return {"status": "error", "details": detail, "task_id": task_id}

        if not entry:
            print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> PENDING")
            return {"status": "pending", "task_id": task_id}

        failure = _comfy_failure_detail(entry)
        if failure:
            active_tasks.pop(task_id, None)
            print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> ERROR: {failure}")
            return {"status": "error", "details": failure, "task_id": task_id}

        outputs = entry.get("outputs", {})

        output_info = None
        output_type = None
        for node_output in outputs.values():
            if "images" in node_output and len(node_output["images"]) > 0:
                output_info = node_output["images"][0]
                output_type = "image"
                break
            if "gifs" in node_output and len(node_output["gifs"]) > 0:
                output_info = node_output["gifs"][0]
                output_type = "video"
                break

        if not output_info:
            status_info = entry.get("status") or {}
            if isinstance(status_info, dict) and status_info.get("completed") and status_info.get("status_str") == "success":
                active_tasks.pop(task_id, None)
                detail = "ComfyUI finished without an image or video output"
                print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> ERROR: {detail}")
                return {"status": "error", "details": detail, "task_id": task_id}
            print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> PENDING")
            return {"status": "pending", "task_id": task_id}

        params = {
            "filename": output_info["filename"],
            "subfolder": output_info.get("subfolder", ""),
            "type": output_info.get("type", "output"),
        }
        file_res = await client.get(f"{comfy_http}/view", params=params)

        # M2: save with a stable name (task_id) instead of ComfyUI's counter name
        os.makedirs(GENERATED_DIR, exist_ok=True)
        ext = os.path.splitext(output_info["filename"])[1] or ".png"
        stable_name = f"{task_id}{ext}"
        save_path = os.path.join(GENERATED_DIR, stable_name)
        with open(save_path, "wb") as f:
            f.write(file_res.content)

        media_type = "video/mp4" if output_type == "video" else "image/png"
        print(f"[Status] task_id={task_id} prompt_id={prompt_id} -> COMPLETE ({stable_name}) [{output_type}]")

        return Response(
            content=file_res.content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{stable_name}"',
                "X-Task-Status": "complete",
            },
        )

@router.delete("/status/{task_id}")
async def remove_task(task_id: str):
    """Remove a completed task from the tracker after the frontend confirms receipt."""
    if task_id in active_tasks:
        prompt_id, _ts = active_tasks.pop(task_id)
        print(f"[Cleanup] task_id={task_id} prompt_id={prompt_id} removed (active: {len(active_tasks)})")
        return {"status": "removed", "task_id": task_id}
    return {"status": "not_found", "task_id": task_id}

async def _purge_stale_tasks(db):
    """Remove tasks older than the configured TTL (from ComfyUISettings.taskTTL)."""
    comfyui = await db.comfyuisettings.find_first()
    ttl = comfyui.taskTTL if comfyui and comfyui.taskTTL else DEFAULT_TASK_TTL
    now = time.time()
    stale = [tid for tid, (_pid, ts) in active_tasks.items() if now - ts > ttl]
    for tid in stale:
        del active_tasks[tid]
        print(f"[Cleanup] purged stale task {tid} (ttl={ttl}s)")

@router.get("/active")
async def get_active_tasks(db: Any = Depends(get_db)):
    """Return all active task IDs so the frontend can poll them all."""
    await _purge_stale_tasks(db)
    return {"tasks": list(active_tasks.keys()), "count": len(active_tasks)}

@router.delete("/active")
async def clear_all_tasks(db: Any = Depends(get_db)):
    """Drop tracked tasks and cancel those prompts in ComfyUI."""
    tracked = list(active_tasks.items())
    prompt_ids = [info[0] for _task_id, info in tracked]
    active_tasks.clear()
    await _cancel_comfy_prompts(db, prompt_ids)
    print(f"[Cleanup] Cleared all tasks ({len(tracked)} removed)")
    return {"status": "cleared", "count": len(tracked)}
