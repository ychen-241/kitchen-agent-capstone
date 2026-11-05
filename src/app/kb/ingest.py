"""知识库入库：文档切分 + 向量化（字符 n-gram TF 向量）。

生产替换点：embedding 换 embedding API / BGE 模型，存储换 pgvector；
retriever 接口不变。
"""
import json
import math
import re
from collections import Counter


def _tokenize(text: str) -> Counter:
    text = re.sub(r"\s+", "", text)
    grams: list[str] = []
    for i in range(len(text) - 1):
        grams.append(text[i:i + 2])
    if len(text) == 1:
        grams.append(text)
    return Counter(grams)


def embed(text: str) -> dict:
    """返回归一化稀疏向量 {ngram: weight}。"""
    counter = _tokenize(text)
    norm = math.sqrt(sum(v * v for v in counter.values())) or 1.0
    return {k: round(v / norm, 6) for k, v in counter.items()}


def chunk_text(content: str, size: int = 120) -> list[str]:
    """按句/行切分并聚合为约 size 字的块。"""
    parts = [p.strip() for p in re.split(r"[。\n]", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) + 1 > size:
            chunks.append(buf + "。")
            buf = part
        else:
            buf = f"{buf}。{part}" if buf else part
    if buf:
        chunks.append(buf + "。")
    return chunks


def chunk_and_embed(conn, doc_id: int, content: str) -> int:
    """写入 kb_chunks（先清旧），返回块数。"""
    conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
    count = 0
    for idx, chunk in enumerate(chunk_text(content)):
        vec = embed(chunk)
        conn.execute(
            "INSERT INTO kb_chunks (doc_id, chunk_index, content, embedding, tokens) VALUES (?,?,?,?,?)",
            (doc_id, idx, chunk, json.dumps(vec, ensure_ascii=False), len(chunk)),
        )
        count += 1
    return count
