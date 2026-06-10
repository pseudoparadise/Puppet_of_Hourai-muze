"""
encoder.py - 豆包 Embedding API 封装 + FAISS 索引管理（修复版）
已按导演2026-05-01后期指示切换为 requests 实现，使用方舟多模态端点。

FIX #1: 使用 requests.Session() 复用 TCP 连接，解决首次调用 ConnectionResetError(10054)
FIX #2: 添加指数退避重试（最多3次）
FIX #3: 引入 id_map.json 双向映射系统，解决字符串ID（如 "20260506_约定去海边"）无法作为FAISS int64 ID的问题
ER-1: save_index/load_index 通过 model_meta.json 校验 key_fingerprint 防止密钥更换后语义空间错乱
NEW: 预留 _score_card() 打分扩展点，配合 retriever 未来重排优化
"""
import numpy as np
import faiss
import os
import json
import time
import re
import requests
import hashlib

DIM = 2048
INDEX_PATH = os.path.join(os.path.dirname(__file__), "vectors.faiss")
ID_MAP_PATH = os.path.join(os.path.dirname(__file__), "id_map.json")
MODEL_META_PATH = os.path.join(os.path.dirname(__file__), "model_meta.json")

# ── NEW: 全局 Session，复用 TCP 连接，避免每次新建 TLS 握手被远端 Reset ──
_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Content-Type": "application/json"})
        # 连接池复用，降低首连失败率
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=8,
            max_retries=0  # 我们自己控制重试
        )
        _session.mount("https://", adapter)
    return _session

# ── NEW: ID 映射管理（字符串ID ↔ FAISS int64 ID） ──
def _load_id_map():
    default = {"str_to_int": {}, "int_to_str": {}, "next_int": 1}
    if os.path.exists(ID_MAP_PATH):
        try:
            with open(ID_MAP_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "str_to_int" in d and "int_to_str" in d:
                d.setdefault("next_int", 1)
                return d
        except Exception:
            pass
    return default

def _save_id_map(id_map):
    import tempfile, shutil
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="atomic_idmap_", dir=os.path.dirname(ID_MAP_PATH))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(id_map, f, ensure_ascii=False, indent=2)
        shutil.move(tmp_path, ID_MAP_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def _register_id(id_map, str_id: str) -> int:
    """注册字符串ID，返回对应的 int64 ID"""
    if str_id in id_map["str_to_int"]:
        return id_map["str_to_int"][str_id]
    int_id = id_map["next_int"]
    id_map["str_to_int"][str_id] = int_id
    id_map["int_to_str"][str(int_id)] = str_id
    id_map["next_int"] = int_id + 1
    return int_id

def _str_id_to_int(str_id: str) -> int:
    """字符串ID → FAISS int64 ID，不存在则返回 None"""
    id_map = _load_id_map()
    return id_map["str_to_int"].get(str_id)

def _int_id_to_str(int_id: int) -> str:
    """FAISS int64 ID → 字符串ID，不存在则返回 None"""
    id_map = _load_id_map()
    return id_map["int_to_str"].get(str(int_id))

# ── 配置读取（缓存以避免重复读 YAML） ──
_config_cache = None

def _get_config():
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    # ── 毒点20修复：从 config.json 读取 volcengine 配置 ──
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = json.load(f)
    return _config_cache

def _get_api_key():
    cfg = _get_config()
    return cfg["volcengine"]["ark_api_key"]

# ── FIX: 添加重试 + Session 复用 ──
def embed(text: str, max_retries: int = 3) -> np.ndarray:
    """
    调用豆包多模态 Embedding API，返回 shape=(2048,) 的 float32 numpy 数组。
    使用 requests.Session 复用连接 + 指数退避重试，解决首次 ConnectionResetError。
    """
    api_key = _get_api_key()
    session = _get_session()

    # 每次请求前刷新 Authorization（Session 会保留其他 header）
    headers = {"Authorization": f"Bearer {api_key}"}

    body = {
        "model": "doubao-embedding-vision-250615",
        "input": [
            {"type": "text", "text": text}
        ]
    }

    url = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = session.post(url, headers=headers, json=body, timeout=30)
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

        except requests.exceptions.ConnectionError as e:
            # ── FIX: ConnectionResetError 处理方法：退避重试 ──
            last_error = e
            wait = 1.0 * (2 ** attempt)  # 1s, 2s, 4s
            print(f"[encoder] 连接失败，第 {attempt+1}/{max_retries} 次重试，等待 {wait:.1f}s: {e}")
            time.sleep(wait)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 1.0 * (2 ** attempt)
                print(f"[encoder] 请求失败，第 {attempt+1}/{max_retries} 次重试，等待 {wait:.1f}s: {e}")
                time.sleep(wait)
            else:
                raise

    raise ConnectionError(f"embed 调用失败（已重试 {max_retries} 次）: {last_error}")

def extract_summary(content: str) -> str:
    """从卡片正文提取「概括」部分。匹配 '概括：...' 到文末。"""
    if not content:
        return ""
    m = re.search(r'(?:\s*[|‖]\s*概括[：:]|\n概括[：:])(.*?)\Z', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def extract_quote(content: str) -> str:
    """从卡片正文提取「原话」部分。匹配 '原话：... | 概括：' 或 '原话：...概括：'。"""
    if not content:
        return ""
    m = re.search(r'原话[：:](.*?)(?:\s*[|‖]\s*概括[：:]|\n概括[：:]|\Z)', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return content[:200]


def build_embed_summary(card: dict) -> str:
    """摘要向量输入: title + 概括。FAISS 检索主通道。"""
    title = card.get("title", "")
    content = card.get("content", "")
    category = card.get("category", "")

    if category == "erotic":
        return (title + " " + (content or ""))[:512]

    summary = extract_summary(content)
    parts = [p for p in [title, summary] if p]
    return " ".join(parts)[:1024]


def build_embed_kw(card: dict) -> str:
    """关键词向量输入: title + keywords。退化 TITLE 阶段余弦比对用。"""
    title = card.get("title", "")
    keywords = card.get("keywords", "")
    parts = [p for p in [title, keywords] if p]
    return " ".join(parts)[:512]


def build_embed_quote(card: dict) -> str:
    """原话向量输入: title + user_raw。退化 QUOTE 阶段余弦比对用。"""
    title = card.get("title", "")
    user_raw = card.get("user_raw", "")
    parts = [p for p in [title, user_raw] if p]
    return " ".join(parts)[:512]


def build_embed_text(card: dict) -> str:
    """[兼容别名] 等同于 build_embed_summary。旧调用点不改也能跑。"""
    return build_embed_summary(card)


def create_index() -> faiss.Index:
    base_index = faiss.IndexFlatL2(DIM)
    return faiss.IndexIDMap(base_index)

# ── FIX: 使用 ID 映射，不再直接 int(card_id) ──
def add_to_index(index: faiss.Index, card_id: str, vector: np.ndarray):
    """
    将卡片向量加入 FAISS 索引。
    自动通过 id_map.json 将字符串ID映射为 int64。
    """
    id_map = _load_id_map()
    int_id = _register_id(id_map, card_id)
    index.add_with_ids(
        np.array([vector], dtype=np.float32),
        np.array([int_id], dtype=np.int64)
    )
    _save_id_map(id_map)

# ── FIX: 搜索结果自动反向映射回字符串ID ──
def search_index(index: faiss.Index, query_vector: np.ndarray, k: int = 5) -> list:
    """
    搜索返回 [(str_card_id, distance), ...]。
    自动将 FAISS int64 ID 反向映射为字符串ID。
    """
    id_map = _load_id_map()
    distances, ids = index.search(np.array([query_vector], dtype=np.float32), k)
    results = []
    for i in range(len(ids[0])):
        int_id = ids[0][i]
        if int_id != -1:
            str_id = id_map["int_to_str"].get(str(int_id), str(int_id))
            results.append((str_id, float(distances[0][i])))
    return results

def save_index(index: faiss.Index):
    faiss.write_index(index, INDEX_PATH)
    # ── P2-4: 保存模型元数据（DIM + 模型名 + 密钥指纹） ──
    api_key = _get_api_key()
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    meta = {"dim": DIM, "model": "doubao-embedding-vision-250615", "key_fingerprint": fingerprint}
    import tempfile as _tmp_m, shutil as _sh_m
    _fd_m, _tp_m = _tmp_m.mkstemp(suffix=".json", prefix="atomic_meta_", dir=os.path.dirname(MODEL_META_PATH))
    try:
        with os.fdopen(_fd_m, "w", encoding="utf-8") as _f_m:
            json.dump(meta, _f_m, ensure_ascii=False, indent=2)
        _sh_m.move(_tp_m, MODEL_META_PATH)
    except Exception:
        if os.path.exists(_tp_m):
            os.unlink(_tp_m)
        raise

def load_index() -> faiss.Index:
    # ── P2-4: 检查模型版本兼容性 ──
    if os.path.exists(MODEL_META_PATH):
        with open(MODEL_META_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("dim", DIM) != DIM:
            raise ValueError(
                f"[encoder] 模型维度不匹配！当前 DIM={DIM}，索引中 DIM={meta.get('dim')}。"
                f"请删除 {INDEX_PATH} 和 {MODEL_META_PATH} 后重建索引。"
            )
        stored_fp = meta.get("key_fingerprint")
        if stored_fp:
            current_fp = hashlib.sha256(_get_api_key().encode()).hexdigest()[:8]
            if stored_fp != current_fp:
                raise ValueError(
                    f"[encoder] key_fingerprint 不匹配！索引由密钥 {stored_fp} 生成，"
                    f"当前密钥指纹为 {current_fp}。"
                    f"请删除 {INDEX_PATH}、{ID_MAP_PATH} 和 {MODEL_META_PATH} 後重建索引。"
                )
    if os.path.exists(INDEX_PATH):
        index = faiss.read_index(INDEX_PATH)
        # ── 毒点38修复：校验索引维度 ──
        if index.d != DIM:
            raise ValueError(
                f"[encoder] FAISS 索引维度不匹配！索引 d={index.d}，代码 DIM={DIM}。"
                f"请删除 {INDEX_PATH} 后重建索引。"
            )
        return index
    return create_index()

# ── NEW: 从索引中移除指定字符串ID ──
def remove_from_index(str_id: str):
    """从 FAISS 索引中移除一张卡片，并清理 id_map"""
    id_map = _load_id_map()
    int_id = id_map["str_to_int"].pop(str_id, None)
    if int_id is not None:
        id_map["int_to_str"].pop(str(int_id), None)
        _save_id_map(id_map)
        try:
            index = load_index()
            index.remove_ids(np.array([int_id], dtype=np.int64))
            save_index(index)
        except Exception as e:
            print(f"[encoder] 索引移除失败（id_map已清理）: {e}")