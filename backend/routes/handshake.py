from fastapi import APIRouter, Depends
from typing import Any, Optional
import httpx
from database import get_db
from models import HandshakeResponse

router = APIRouter()


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    # Ignore HTTP_PROXY so LAN addresses like 192.168.x.x are not sent through a local proxy.
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def _probe_ollama(client: httpx.AsyncClient, base: str) -> Optional[dict]:
    """Return a normalized model list when the server is Ollama, otherwise None."""
    try:
        tags_resp = await client.get(f"{base}/api/tags")
    except httpx.HTTPError:
        return None
    if tags_resp.status_code != 200:
        return None
    try:
        models = tags_resp.json().get("models")
    except ValueError:
        return None
    if not isinstance(models, list):
        return None
    if models and not (isinstance(models[0], dict) and "name" in models[0] and "loaded_instances" not in models[0]):
        return None

    running: set[str] = set()
    try:
        ps_resp = await client.get(f"{base}/api/ps")
        if ps_resp.status_code == 200:
            for item in ps_resp.json().get("models", []):
                name = item.get("name") or item.get("model")
                if name:
                    running.add(name)
    except (httpx.HTTPError, ValueError):
        pass

    normalized = []
    for item in models:
        name = item.get("name") or item.get("model") or "unknown"
        size = (item.get("details") or {}).get("parameter_size")
        normalized.append({
            "key": name,
            "display_name": f"{name} · {size}" if size else name,
            "loaded_instances": [{"id": name}] if name in running else [],
        })
    return {"models": normalized, "running": running}


def _ollama_status(model_name: str, probe: dict, ip: str, port: int) -> dict:
    models = probe["models"]
    names = [m["key"] for m in models]
    if not names:
        return {
            "status": "no_models",
            "server_reachable": True,
            "active_model": "No Model Loaded",
            "ping_success": True,
            "details": f"Ollama at {ip}:{port} is reachable, but no models are pulled.",
            "models": models,
        }
    if model_name and model_name not in names:
        return {
            "status": "unhealthy",
            "server_reachable": True,
            "active_model": model_name,
            "ping_success": True,
            "details": f"Model '{model_name}' is not on this Ollama server.",
            "models": models,
        }
    active = model_name or names[0]
    in_memory = active in probe["running"]
    memory = "loaded in memory" if in_memory else "will load on the first request"
    return {
        "status": "healthy",
        "server_reachable": True,
        "active_model": active,
        "ping_success": True,
        "details": f"Connected to Ollama at {ip}:{port}. {active} {memory}.",
        "models": models,
    }


def _lmstudio_status(model_list: list, ip: str, port: int, connected_detail: str) -> dict:
    loaded_models = [m for m in model_list if m.get("loaded_instances")]
    if not loaded_models:
        return {
            "status": "no_models",
            "server_reachable": True,
            "active_model": "No Model Loaded",
            "ping_success": True,
            "details": "LM Studio is ready, but no models are loaded.",
            "models": model_list,
        }
    first = loaded_models[0]
    active_model = first.get("display_name", first.get("key", "Unnamed Model"))
    return {
        "status": "healthy",
        "server_reachable": True,
        "active_model": active_model,
        "ping_success": True,
        "details": connected_detail or f"Connected to LM Studio at {ip}:{port}",
        "models": model_list,
    }


async def _inspect(ip: str, port: int, model_name: str, timeout: httpx.Timeout) -> dict:
    base = f"http://{ip}:{port}"
    async with _client(timeout) as client:
        ollama = await _probe_ollama(client, base)
        if ollama is not None:
            return _ollama_status(model_name, ollama, ip, port)

        response = await client.get(f"{base}/api/v1/models")
        if response.status_code != 200:
            return {
                "status": "unhealthy",
                "server_reachable": False,
                "active_model": "unknown",
                "ping_success": False,
                "details": f"LLM server responded with HTTP {response.status_code}",
            }
        model_list = response.json().get("models", [])
        return _lmstudio_status(model_list, ip, port, "LM Studio is ready.")


@router.get("/handshake")
async def check_llm_health(db = Depends(get_db)):
    llm = await db.llmsettings.find_first()
    if not llm:
        ip, port, model_name = "127.0.0.1", 11434, ""
    else:
        ip, port, model_name = llm.ip, llm.port, llm.modelName or ""

    timeout = httpx.Timeout(8.0, connect=3.0)
    try:
        result = await _inspect(ip, port, model_name, timeout)
    except Exception as e:
        return HandshakeResponse(
            status="unhealthy",
            server_reachable=False,
            active_model="unknown",
            ping_success=False,
            details=f"{type(e).__name__}: {e}",
        )
    return HandshakeResponse(
        status=result["status"],
        server_reachable=result["server_reachable"],
        active_model=result["active_model"],
        ping_success=result["ping_success"],
        details=result["details"],
    )


@router.post("/llm/load")
async def load_llm(payload: dict = {}, db = Depends(get_db)):
    """Load the configured model. Ollama warms it into memory; LM Studio uses its load API."""
    llm = await db.llmsettings.find_first()
    if not llm:
        return {"status": "error", "details": "No LLM settings found"}

    model_id = payload.get("model") or llm.modelName
    if not model_id:
        return {"status": "error", "details": "No model specified"}

    ip, port = llm.ip, llm.port
    base = f"http://{ip}:{port}"
    timeout = httpx.Timeout(300.0, connect=5.0)

    async with _client(timeout) as client:
        try:
            ollama = await _probe_ollama(client, base)
            if ollama is not None:
                resp = await client.post(f"{base}/api/generate", json={
                    "model": model_id,
                    "prompt": "ok",
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"num_predict": 1},
                })
                resp.raise_for_status()
                return {"status": "success", "model": model_id, "details": "Model loaded in Ollama"}

            resp = await client.post(f"{base}/api/v1/models/load", json={"model": model_id})
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "model": model_id, "details": data.get("status", "Model loaded")}
        except Exception as e:
            return {"status": "error", "details": str(e)}


@router.post("/llm/unload")
async def unload_llm(db = Depends(get_db)):
    """Unload the active model. Ollama uses keep_alive=0; LM Studio uses its unload API."""
    llm = await db.llmsettings.find_first()
    if not llm:
        return {"status": "error", "details": "No LLM settings found"}

    ip, port = llm.ip, llm.port
    base = f"http://{ip}:{port}"
    timeout = httpx.Timeout(30.0, connect=3.0)

    async with _client(timeout) as client:
        try:
            ollama = await _probe_ollama(client, base)
            if ollama is not None:
                running = list(ollama["running"])
                target = llm.modelName if llm.modelName in ollama["running"] else (running[0] if running else None)
                if not target:
                    return {"status": "success", "details": "No Ollama model is loaded in memory"}
                resp = await client.post(f"{base}/api/generate", json={"model": target, "keep_alive": 0})
                resp.raise_for_status()
                return {"status": "success", "model": target, "details": "Model unloaded"}

            resp = await client.get(f"{base}/api/v1/models")
            resp.raise_for_status()
            model_list = resp.json().get("models", [])
            loaded_models = [m for m in model_list if m.get("loaded_instances")]
            if not loaded_models:
                return {"status": "error", "details": "No models loaded"}
            instance_id = loaded_models[0]["loaded_instances"][0].get("id", "")
            unload_resp = await client.post(f"{base}/api/v1/models/unload", json={"instance_id": instance_id})
            unload_resp.raise_for_status()
            return {"status": "success", "model": instance_id, "details": "Model unloaded"}
        except Exception as e:
            return {"status": "error", "details": str(e)}


@router.get("/llm-test")
async def test_llm(ip: Optional[str] = None, port: Optional[int] = None, db = Depends(get_db)):
    llm = await db.llmsettings.find_first()
    model_name = llm.modelName if llm else ""
    if not ip or not port:
        if not llm:
            return {"status": "error", "details": "No LLM settings found in database."}
        ip, port = llm.ip, llm.port

    timeout = httpx.Timeout(10.0, connect=3.0)
    try:
        return await _inspect(ip, port, model_name or "", timeout)
    except Exception as e:
        return {
            "status": "unhealthy",
            "server_reachable": False,
            "ping_success": False,
            "details": f"{type(e).__name__}: {e}",
        }
