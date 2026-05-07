"""
encoder.py - 豆包 Embedding API 封装 + FAISS 索引管理
已按导演2026-05-01后期指示切换为 requests 实现，使用方舟多模态端点。
"""
import numpy as np
import faiss
import os
import requests
import yaml

DIM = 2048
INDEX_PATH = os.path.join(os.path.dirname(__file__), "vectors.faiss")

def _get_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg

def _get_api_key():
    """从 config.yaml 读取 ark_api_key"""
    cfg = _get_config()
    return cfg["volcengine"]["ark_api_key"]

def embed(text: str) -> np.ndarray:
    """
    调用豆包多模态 Embedding API，返回 shape=(2048,) 的 float32 numpy 数组。
    使用方舟多模态端点，严格按官方文档格式构造请求体。
    """
    api_key = _get_api_key()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    body = {
        "model": "doubao-embedding-vision-250615",
        "input": [
            {"type": "text", "text": text}
        ]
    }

    url = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    embedding_data = result["data"]
    if isinstance(embedding_data, dict):
        vec = embedding_data["embedding"]
    elif isinstance(embedding_data, list):
        vec = embedding_data[0]["embedding"]
    else:
        raise ValueError(f"未知返回结构: {type(embedding_data)}")

    arr = np.array(vec, dtype=np.float32)
    if arr.shape[0] != DIM:
        raise ValueError(f"维度校验失败：期望 {DIM}，实际 {arr.shape[0]}")
    return arr

def create_index() -> faiss.Index:
    base_index = faiss.IndexFlatL2(DIM)
    return faiss.IndexIDMap(base_index)

def add_to_index(index: faiss.Index, card_id: str, vector: np.ndarray):
    index.add_with_ids(np.array([vector], dtype=np.float32), np.array([int(card_id)], dtype=np.int64))

def search_index(index: faiss.Index, query_vector: np.ndarray, k: int = 5) -> list:
    distances, ids = index.search(np.array([query_vector], dtype=np.float32), k)
    results = []
    for i in range(len(ids[0])):
        cid = ids[0][i]
        if cid != -1:
            results.append((str(cid), float(distances[0][i])))
    return results

def save_index(index: faiss.Index):
    faiss.write_index(index, INDEX_PATH)

def load_index() -> faiss.Index:
    if os.path.exists(INDEX_PATH):
        return faiss.read_index(INDEX_PATH)
    return create_index()