# bot/retrieval.py
from typing import List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, or_
from db import Doc

# ——— stoplist PL ———
POLISH_STOP = {
    "i","oraz","a","w","we","z","za","na","do","u","o","po","od","dla","przez",
    "ten","ta","to","te","jest","są","być","się","że","czy","albo","lub",
    "szybki","szybkie","szybka","obiad","śniadanie","lunch","kolacja","przepis","light"
}

# ——— tokenizacja + „prefiksowy stem” ———
def _keywords(q: str) -> List[str]:
    import re
    terms = [t.lower() for t in re.findall(r"[0-9a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", q or "")]
    terms = [t for t in terms if t not in POLISH_STOP and len(t) > 2]
    prefixed = []
    for t in terms:
        base = t[:6] if len(t) >= 7 else (t[:4] if len(t) >= 5 else t)
        prefixed.append(base)
    out = []
    for p in prefixed:
        if p not in out:
            out.append(p)
    return out[:6]

def _fts_query_or_prefix(terms: List[str]) -> str:
    if not terms:
        return ""
    return " OR ".join(f"{t}*" for t in terms)

# ——— /plan: ogólne snippety (TY MUSISZ MIEĆ TĘ FUNKCJĘ) ———
async def search_snippets(session: AsyncSession, query: str, k: int = 8) -> List[Tuple[str, str]]:
    terms = _keywords(query)
    fts_q = _fts_query_or_prefix(terms)

    # 1) hit po tytule
    if terms:
        like_title_sql = select(Doc).where(Doc.title.ilike(f"%{terms[0]}%")).limit(k)
        docs_title = (await session.execute(like_title_sql)).scalars().all()
        if docs_title:
            return [(d.title, (d.content or "")[:800]) for d in docs_title]

    # 2) FTS5 + bm25
    if fts_q:
        try:
            sql = text("""
                SELECT d.id, d.title
                FROM docs_fts
                JOIN docs d ON d.id = docs_fts.rowid
                WHERE docs_fts MATCH :q
                ORDER BY bm25(docs_fts) ASC
                LIMIT :k
            """)
            rows = (await session.execute(sql, {"q": fts_q, "k": k})).all()
            if rows:
                ids = [r[0] for r in rows]
                docs = (await session.execute(select(Doc).where(Doc.id.in_(ids)))).scalars().all()
                id2doc = {d.id: d for d in docs}
                out = []
                for rid, title in rows:
                    d = id2doc.get(rid)
                    content = (d.content or "")[:800] if d else ""
                    out.append((title, content))
                return out
        except Exception:
            pass

    # 3) Fallback LIKE
    if terms:
        likes = [Doc.content.ilike(f"%{t}%") for t in terms]
        docs = (await session.execute(select(Doc).where(or_(*likes)).limit(k))).scalars().all()
        if docs:
            return [(d.title, (d.content or "")[:800]) for d in docs]

    # 4) awaryjnie
    docs = (await session.execute(select(Doc).limit(3))).scalars().all()
    return [(d.title, (d.content or "")[:800]) for d in docs]

# ——— wyszukiwanie po rodzaju + meta ———
async def search_by_kind(session: AsyncSession, query: str, kind: str, k: int = 5):
    terms = _keywords(query)
    fts_q = _fts_query_or_prefix(terms) or query

    # FTS5 z filtrem kind
    try:
        sql = text("""
            SELECT d.id, d.title
            FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH :q
              AND d.kind = :kind
            ORDER BY bm25(docs_fts) ASC
            LIMIT :k
        """)
        rows = (await session.execute(sql, {"q": fts_q, "kind": kind, "k": k})).all()
        if rows:
            ids = [r[0] for r in rows]
            docs = (await session.execute(select(Doc).where(Doc.id.in_(ids)))).scalars().all()
            id2doc = {d.id: d for d in docs}
            out = []
            for rid, title in rows:
                d = id2doc.get(rid)
                content = (d.content or "")[:800] if d else ""
                meta = d.meta or {}
                out.append((title, content, meta))
            return out
    except Exception:
        pass

    # Fallback: LIKE
    likes = [Doc.content.ilike(f"%{t}%") for t in terms] if terms else []
    if likes:
        docs = (await session.execute(
            select(Doc).where(Doc.kind == kind, or_(*likes)).limit(k)
        )).scalars().all()
    else:
        docs = (await session.execute(
            select(Doc).where(Doc.kind == kind).limit(k)
        )).scalars().all()

    return [(d.title, (d.content or "")[:800], (d.meta or {})) for d in docs]

# ——— wyszukiwanie po rodzaju + filtr meta.topic ———
async def search_by_kind_topic(session: AsyncSession, query: str, kind: str, topic: str, k: int = 3):
    """
    FTS5 + filtr meta.topic:
      - d.kind == kind (ebook/note/study/recipe)
      - json_extract(d.meta, '$.topic') == topic  (np. 'Trening', 'Redukcja', 'Masa', 'Motywacja')
    Jeśli nic nie znajdzie → LIKE po meta → fallback do search_by_kind().
    """
    terms = _keywords(query)
    fts_q = _fts_query_or_prefix(terms) or query

    # 1) FTS5 + meta.topic
    try:
        sql = text("""
            SELECT d.id, d.title
            FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH :q
              AND d.kind = :kind
              AND json_extract(d.meta, '$.topic') = :topic
            ORDER BY bm25(docs_fts) ASC
            LIMIT :k
        """)
        rows = (await session.execute(sql, {"q": fts_q, "kind": kind, "topic": topic, "k": k})).all()
        if rows:
            ids = [r[0] for r in rows]
            docs = (await session.execute(select(Doc).where(Doc.id.in_(ids)))).scalars().all()
            id2 = {d.id: d for d in docs}
            out = []
            for rid, title in rows:
                d = id2.get(rid)
                content = (d.content or "")[:800] if d else ""
                meta = d.meta or {}
                out.append((title, content, meta))
            return out
    except Exception:
        pass

    # 2) Fallback: LIKE po meta.topic
    try:
        like_topic = f'%\"topic\": \"{topic}\"%'
        docs = (await session.execute(
            select(Doc).where(Doc.kind == kind, Doc.meta.ilike(like_topic)).limit(k)
        )).scalars().all()
        if docs:
            return [(d.title, (d.content or "")[:800], (d.meta or {})) for d in docs]
    except Exception:
        pass

    # 3) Ostatecznie: zwykłe wyszukiwanie po kind
    return await search_by_kind(session, query, kind, k=k)
