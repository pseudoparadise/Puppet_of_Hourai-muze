import os
import json
import base64

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def _load_vision_config():
    cfg_path = os.path.join(PROJECT_ROOT, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        vc = cfg.get("volcengine", {})
        return {
            "api_key": vc.get("ark_api_key", ""),
            "model": vc.get("vision_model", ""),
            "base_url": vc.get("vision_base_url", "https://ark.cn-beijing.volces.com/api/v3"),
        }
    except Exception:
        return {}

_VISION_CFG = _load_vision_config()


def _img_to_data_url(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/png")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _get_client(timeout=45):
    from openai import OpenAI
    cfg = _load_vision_config()
    return OpenAI(
        base_url=cfg.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
        api_key=cfg.get("api_key", ""),
        timeout=timeout,
    ), cfg.get("model", "")


def ask(image_path: str, prompt: str, timeout: int = 45) -> str:
    client, model = _get_client(timeout)
    data_url = _img_to_data_url(image_path)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        max_tokens=1024,
    )
    return resp.choices[0].message.content


def ask_url(image_url: str, prompt: str, timeout: int = 45) -> str:
    client, model = _get_client(timeout)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}],
        max_tokens=1024,
    )
    return resp.choices[0].message.content
