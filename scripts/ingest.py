import os, re, csv
from sqlalchemy import select, delete
from db import Session, Doc, engine, ensure_schema

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def chunk_text(text: str, size=800, overlap=120):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text: return [""]
    chunks, i = [], 0
    while i < len(text):
        end = min(len(text), i + size)
        chunks.append(text[i:end])
        i += max(1, size - overlap)
    return chunks

async def ingest_txt_file(path: str, kind: str, title: str, meta: dict | None = None):
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    for ch in chunk_text(txt):
        async with Session() as s:
            s.add(Doc(kind=kind, title=title, meta=meta or {}, content=ch))
            await s.commit()

async def ingest_folder_txt(folder: str, kind: str, topic: str | None = None):
    folder_path = os.path.join(DATA_DIR, folder)
    if not os.path.isdir(folder_path): return 0
    count = 0
    for name in sorted(os.listdir(folder_path)):
        if not name.lower().endswith(".txt"): continue
        path = os.path.join(folder_path, name)
        meta = {"topic": topic or folder, "source": name}
        title = f"{(topic or folder)}: {os.path.splitext(name)[0]}"
        await ingest_txt_file(path, kind=kind, title=title, meta=meta)
        count += 1
    return count

async def ingest_ebooks_root():
    loaded = 0
    for name in os.listdir(DATA_DIR):
        low = name.lower()
        if low.endswith(".txt") and low.startswith("ebook"):
            path = os.path.join(DATA_DIR, name)
            title = os.path.splitext(name)[0]
            await ingest_txt_file(path, kind="ebook", title=title, meta={"source": name})
            loaded += 1
    return loaded

async def ingest_recipes():
    # użyj recipes.csv jeśli podmienisz; na razie zostawimy sample gdy jest
    pref = os.path.join(DATA_DIR, "recipes.csv")
    sample = os.path.join(DATA_DIR, "recipes.sample.csv")
    path = pref if os.path.exists(pref) else (sample if os.path.exists(sample) else None)
    if not path: return 0
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
    async with Session() as s:
        for row in rows:
            content = (
                f"Przepis: {row['title']} | kcal: {row['kcal']} | B: {row['protein']} | "
                f"W: {row['carbs']} | T: {row['fat']}\n"
                f"Składniki: {row.get('ingredients','')}\nKroki: {row.get('steps','')}"
            )
            meta = {"url": row.get("url",""), "tags": row.get("tags","")}
            s.add(Doc(kind="recipe", title=row["title"], meta=meta, content=content))
        await s.commit()
    return len(rows)

async def main():
    await ensure_schema()
    # czyść wszystko (żeby nowa baza była spójna)
    async with Session() as s:
        await s.execute(delete(Doc)); await s.commit()

    # 1) EBOOKI w głównym katalogu
    ebooks_n = await ingest_ebooks_root()

    # 2) NOTATKI TEMATYCZNE
    notes_total = 0
    for topic in ["Masa", "Motywacja", "Redukcja", "Trening"]:
        notes_total += await ingest_folder_txt(topic, kind="note", topic=topic)

    # 3) (opcjonalnie) books/ i studies/
    if os.path.isdir(os.path.join(DATA_DIR, "books")):
        await ingest_folder_txt("books", kind="book", topic="books")
    if os.path.isdir(os.path.join(DATA_DIR, "studies")):
        await ingest_folder_txt("studies", kind="study", topic="studies")

    # 4) Przepisy (na razie sample; później podmienisz na recipes.csv)
    recipes_n = await ingest_recipes()

    # 5) Odśwież FTS (SQLite)
    if "sqlite" in engine.url.render_as_string():
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DELETE FROM docs_fts;")
            await conn.exec_driver_sql(
                "INSERT INTO docs_fts(rowid, title, content) "
                "SELECT id, title, content FROM docs;"
            )

    # Statystyki
    async with Session() as s:
        docs = (await s.execute(select(Doc))).scalars().all()
    by_kind = {}
    for d in docs:
        by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
    print("Ingest done ✅")
    print("Docs by kind:", by_kind)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
