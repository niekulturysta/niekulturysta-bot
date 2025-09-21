# bot/retrieval.py
from typing import List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from db import Doc
from sqlalchemy import text, select, or_, cast, String


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

async def search_by_kind(session: AsyncSession, bias, kind: str, k: int = 3):
    """
    Prosty LIKE pod Postgresa: szuka w treści dokumentów o danym kind
    według słów kluczowych z 'bias' (lista/str). Zwraca (title, snippet, meta).
    """
    try:
        terms = []
        if isinstance(bias, list):
            terms = [w for w in bias[:6] if w]
        elif isinstance(bias, str) and bias.strip():
            terms = [bias.strip()]

        filters = [Doc.content.ilike(f"%{w}%") for w in terms]
        stmt = (
            select(Doc)
            .where(
                Doc.kind == kind,
                or_(*filters) if filters else True  # gdy brak słów – nie filtruj treści
            )
            .limit(k)
        )
        res = await session.execute(stmt)
        rows = res.scalars().all()
        return [(d.title, (d.content or "")[:800], (d.meta or {})) for d in rows]
    except SQLAlchemyError:
        await session.rollback()
        return []

async def search_by_kind_topic(session: AsyncSession, bias, kind: str, topic: str, k: int = 3):
    """
    Wersja 'topic' pod Postgresa: filtruje po kind oraz po meta.topic (JSON),
    ale przez prosty LIKE na zcastowanym do tekstu JSON-ie.
    """
    try:
        like_topic = f'%\"topic\": \"{topic}\"%'
        terms = []
        if isinstance(bias, list):
            terms = [w for w in bias[:6] if w]
        elif isinstance(bias, str) and bias.strip():
            terms = [bias.strip()]
        content_filters = [Doc.content.ilike(f"%{w}%") for w in terms]

        stmt = (
            select(Doc)
            .where(
                Doc.kind == kind,
                cast(Doc.meta, String).ilike(like_topic),
                or_(*content_filters) if content_filters else True
            )
            .limit(k)
        )
        res = await session.execute(stmt)
        rows = res.scalars().all()
        if rows:
            return [(d.title, (d.content or "")[:800], (d.meta or {})) for d in rows]
    except SQLAlchemyError:
        await session.rollback()

    # fallback: bez topic
    return await search_by_kind(session, bias, kind, k=k)

