"""向量检索：余弦相似度 top-k（只检索已发布文档）。"""
import json

from .ingest import embed


def search(conn, query: str, top_k: int = 3, min_score: float = 0.05) -> list[dict]:
    q = embed(query)
    rows = conn.execute(
        "SELECT c.id, c.doc_id, c.chunk_index, c.content, c.embedding, d.title "
        "FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id "
        "WHERE d.status = 'published'"
    ).fetchall()
    scored = []
    for row in rows:
        vec = json.loads(row["embedding"])
        common = set(q) & set(vec)
        if not common:
            continue
        score = sum(q[k] * vec[k] for k in common)
        if score >= min_score:
            scored.append({
                "doc_id": row["doc_id"],
                "doc_title": row["title"],
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "score": round(score, 4),
            })
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]
