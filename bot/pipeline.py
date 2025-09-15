import json
from openai import OpenAI
from settings import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select

from .retrieval import search_by_kind, search_by_kind_topic
from .utils import infer_targets, violates_targets

client = OpenAI(api_key=settings.openai_api_key)

# === Agent A: Planner ===
PLANNER_SYS = (
    "Jesteś planistą fitness. Ekstrahuj z pytania użytkownika zwięzły plan pozyskania danych.\n"
    "Zwróć WYŁĄCZNIE JSON z polami:\n"
    "{topic: 'redukcja'|'masa'|'trening'|'dieta'|'motywacja', "
    " muscles: [string], need_studies: 0|1, level: 'pocz'|'sred'|'zaaw', kcal: number|null}\n"
    "Jeśli czegoś nie ma w pytaniu – oszacuj zdroworozsądkowo, ale nie dodawaj komentarzy poza JSON."
)

async def plan_query(user_q: str) -> dict:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": PLANNER_SYS},
            {"role": "user", "content": user_q},
        ],
        temperature=0,
        max_tokens=250,
    )
    txt = resp.choices[0].message.content.strip()
    # awaryjnie spróbuj wyciąć JSON
    start = txt.find("{"); end = txt.rfind("}")
    if start != -1 and end != -1:
        txt = txt[start:end+1]
    try:
        return json.loads(txt)
    except Exception:
        return {"topic":"trening" if "trening" in user_q.lower() else "dieta", "muscles":[], "need_studies":0, "level":"pocz", "kcal":None}

# === Agent B: Retriever/Verifier ===
def _score_block(title: str, content: str) -> float:
    c = (content or "").lower()
    keys = ["kcal","b:","t:","w:","g/kg","ser","powt","rir","deficyt","nadwyż","if-then","plan","checklista"]
    return sum(1 for k in keys if k in c) + min(len(content), 900)/450.0

def _topk(blocks, k):
    return sorted(blocks, key=lambda x: _score_block(x[0], x[1]), reverse=True)[:k]

async def gather_evidence(session: AsyncSession, plan: dict, user_q: str) -> dict:
    topic = (plan.get("topic") or "").lower()
    need_studies = bool(plan.get("need_studies", 0))
    # notatki celowane
    if topic == "trening":
        notes = await search_by_kind_topic(session, user_q, "note", "Trening", k=8)
    elif topic == "redukcja":
        notes = await search_by_kind_topic(session, user_q, "note", "Redukcja", k=8)
    elif topic == "masa":
        notes = await search_by_kind_topic(session, user_q, "note", "Masa", k=8)
    elif topic == "motywacja":
        notes = await search_by_kind_topic(session, user_q, "note", "Motywacja", k=8)
    else:
        notes = await search_by_kind(session, user_q, "note", k=8)

    ebooks = await search_by_kind(session, user_q, "ebook", k=6)
    studies = await search_by_kind(session, user_q, "study", k=4) if need_studies else []

    # anty off-target
    targets = infer_targets(user_q)
    def filt(blocks):
        out=[]
        for t,c,m in blocks:
            if not violates_targets(f"{t} {c}", targets):
                out.append((t,c,m))
        return out

    notes  = filt(notes)
    ebooks = filt(ebooks)
    studies = filt(studies)

    # wybór topów
    return {
        "ebook": _topk(ebooks, 2),
        "note":  _topk(notes, 3),
        "study": _topk(studies, 2) if studies else []
    }

# === Agent C: Composer ===
COMPOSER_SYS = (
    "Jesteś kompozytorem odpowiedzi Niekulturysta AI. "
    "Odpowiadasz WYŁĄCZNIE na bazie dostarczonych fragmentów (nie dodawaj nowej wiedzy). "
    "Jeśli brakuje informacji — zadaj 1 krótkie pytanie doprecyzowujące zamiast zgadywać.\n"
    "Zakaz: nie proponuj ćwiczeń spoza wskazanych partii.\n"
    "Struktura: \n"
    "1) Twój głos (ebook/notatki) — 5–8 zdań\n"
    "2) Praktyka (notatki/książki) — 5–8 zdań\n"
    "3) Badania — 3–6 zdań (jeśli brak: napisz '— brak dopasowanych badań —')\n"
    "4) Tip (1 zdanie)\n"
    "Cytuj liczby/kroki dosłownie i podawaj źródła w nawiasach kwadratowych."
)

def _pack(blocks):
    return "\n\n".join([f"[{(m.get('source') or t)}] {c}" for t,c,m in blocks]) if blocks else "—"

async def compose_answer(user_q: str, ev: dict) -> str:
    s1 = _pack((ev.get("ebook", []) + ev.get("note", [])[:1])) or "—"
    s2 = _pack(ev.get("note", [])[1:3]) or "—"
    s3 = _pack(ev.get("study", [])) or "— brak dopasowanych badań —"

    user = (
        f"[SEKCJA 1 – Twój głos]\n{s1}\n\n"
        f"[SEKCJA 2 – Praktyka]\n{s2}\n\n"
        f"[SEKCJA 3 – Badania]\n{s3}\n\n"
        f"PYTANIE:\n{user_q}\n"
        "Ułóż odpowiedź w 4 punktach jak w instrukcji."
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":COMPOSER_SYS},
                  {"role":"user","content":user}],
        temperature=0.2,
        max_tokens=1100,
    )
    return resp.choices[0].message.content.strip()

# === Publiczna funkcja: uruchom 3-agentowy pipeline ===
async def run_three_agent(session: AsyncSession, user_q: str) -> str:
    plan = await plan_query(user_q)
    evidence = await gather_evidence(session, plan, user_q)
    # jeżeli nie mamy w ogóle „mięsa”, to pytamy o doprecyzowanie
    if len(evidence.get("ebook",[])) + len(evidence.get("note",[])) == 0:
        return "Potrzebuję doprecyzować: podaj proszę cel (redukcja/masa), poziom (pocz/śred/zaaw) oraz zakres (np. 'trening klatka+barki')."
    ans = await compose_answer(user_q, evidence)
    # szybka kontrola off-target po złożeniu
    targets = infer_targets(user_q)
    if violates_targets(ans, targets):
        # wymuś poprawkę
        fix_q = user_q + " (Usuń ćwiczenia spoza: " + ", ".join(targets) + ")"
        ans = await compose_answer(fix_q, evidence)
    return ans

