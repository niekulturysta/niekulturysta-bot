# bot/ai.py
from openai import OpenAI
from settings import settings

client = OpenAI(api_key=settings.openai_api_key)

def _ctx(profile: dict) -> str:
    """
    Kontekst preferuje warstwę 'current' (bieżące cele),
    a jeśli jej nie ma, używa starych kluczy dla kompatybilności.
    """
    p = profile or {}
    cur = p.get("current") or {}
    kcal = cur.get("kcal") or cur.get("tdee") or p.get("kcal") or p.get("tdee")
    macros = (cur.get("macros") or p.get("macros") or {}) or {}

    return (
        "USER_CTX:\n"
        f"- cel: {cur.get('goal') or p.get('goal','?')}\n"
        f"- TDEE: {cur.get('tdee') or p.get('tdee','?')} kcal\n"
        f"- kcal cel: {kcal or '?'}\n"
        f"- makra (target): B {macros.get('protein_g','?')} g / T {macros.get('fat_g','?')} g / W {macros.get('carbs_g','?')} g\n"
        f"- aktywność: {p.get('baseline',{}).get('activity') or p.get('activity','?')} | trening: {p.get('training','')}\n"
        f"- sen: {p.get('sleep','?')} h | stres: {p.get('stress','?')}\n"
        f"- alergie: {p.get('allergies','brak')} | nielubiane: {p.get('dislikes','brak')} | alkohol/słodycze: {p.get('alcohol','?')}\n"
        f"- priorytet: {p.get('priority','')} | horyzont: {p.get('horizon','')}\n"
    )

BASE_RULES = (
    "Jesteś Niekulturysta AI.\n"
    "Zawsze respektuj USER_CTX (preferuj warstwę 'current').\n"
    "Nie zmieniaj kalorii ani makr użytkownika w odpowiedzi — jeśli trzeba zmieniać cele, sugeruj /raport → /akceptuj.\n"
    "Uwzględniaj alergie i nielubiane – nie proponuj tych składników (i oferuj zamienniki).\n"
    "Gdy brakuje detalu do zadania (np. sprzęt, liczba dni), zadaj MAKSYMALNIE 1 pytanie doprecyzowujące i przyjmij rozsądne założenie.\n"
    "W pytaniach medycznych/czerwonych flagach (ból w klatce, omdlenia, ostre dolegliwości) – zalecaj konsultację lekarską.\n"
)

SAFETY_RAILS = (
    "SZYNY (twarde zasady):\n"
    "- TRENING ≤30 min/sesja: nie proponuj pełnego FBW. Preferuj split 2-grupowy (Push/Pull lub Góra/Dół), "
    "3–4 ćwiczenia łącznie, 2–3 serie, superserie/EMOM/obwody, rozpisz tydzień dniami. "
    "PPL jest NIEDOZWOLONE przy czasie ≤30 min (dopuszczalne dopiero przy ≥45 min).\n"
    "- KONTUZJE/OGRANICZENIA: jeśli w USER_CTX są dolegliwości kręgosłupa/rwa kulszowa, unikaj ciężkiej osiowej kompresji "
    "(np. przysiad tylni, klasyczny martwy ciąg, good morning). Preferuj: trap bar DL, RDL z umiarkowanym ciężarem, hip thrust, "
    "split squat/Bulgarian, leg press/hack, wyciągi/maszyny; technika i RIR 2–3.\n"
    "- JADŁOSPIS 5–7 dni: nie kopiuj jednego dnia ×7. Zastosuj ROTACJE oznaczone literami (śniadania A/B/C, obiady D/E, kolacje F/G). "
    "Trzymaj dzienną kaloryczność w granicach celu z USER_CTX ±10% i podawaj podsumowanie B/T/W dla każdego dnia. "
    "Na końcu dodaj sekcję „LISTA ZAKUPÓW (ZBIORCZA)” zaokrągloną do opakowań (np. ryż 1 kg, oliwa 500 ml).\n"
    "- TREATS (alkohol/słodycze): policz ich kalorie (piwo ≈220 kcal/szt., pączek ≈350 kcal/szt.), odejmij z tygodniowego budżetu "
    "i zredukuj węgle/tłuszcz tak, aby dzienne kcal nadal mieściły się w ±10% celu. Wskaż, w które dni wliczasz treats.\n"
    "- Alergie i nielubiane: bezwzględnie wyklucz; traktuj synonimy jako tożsame (np. drób = kurczak/indyk).\n"
    "- Nie zmieniaj użytkownikowi kalorii ani makr w odpowiedzi. Korekty tylko przez /raport → /akceptuj.\n"
)

def _cap(s: str, n: int = 4000) -> str:
    return s[:n] if s and len(s) > n else (s or "")

async def generate_answer(prompt: str, profile: dict | None = None, snippets: str = "") -> str:
    sys = (
        BASE_RULES +
        SAFETY_RAILS +
        "Styl odpowiedzi:\n"
        "1) KRÓTKO — jedno zdanie wniosków dopasowane do USER_CTX.\n"
        "2) PLAN — 3–6 kroków (lista). Jeśli dotyczy żywienia, trzymaj się celu kcal z USER_CTX (±10%) i makr z USER_CTX. "
        "Jeśli dotyczy treningu i czas ≤30 min, użyj splitu 2-grupowego, 3–4 ćwiczeń łącznie, superserii/EMOM i rozpisz tydzień.\n"
        "3) WSKAZÓWKI — 2–3 krótkie tipy.\n"
        "Nie zmieniaj celu kcal/makr; korekty tylko przez /raport → /akceptuj.\n"
        "Jeśli profil ma cel i kcal — odpowiadaj bez proszenia o /setup; jeśli profil jest pusty — poproś o /setup.\n"
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "system", "content": _ctx(profile or {})},
        {"role": "user", "content": f"FRAGMENTY (opcjonalne):\n{_cap(snippets)}\n\nPYTANIE:\n{prompt}"},
    ]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2,
        max_tokens=800,
    )
    return resp.choices[0].message.content.strip()

async def generate_mealplan(request: str, profile: dict | None = None, snippets: str = "") -> str:
    sys = (
        BASE_RULES +
        SAFETY_RAILS +
        "Zaprojektuj jadłospis zgodny z USER_CTX.\n"
        "Wymagania:\n"
        "- rotacje posiłków (A/B/C dla śniadań, D/E dla obiadów, F/G dla kolacji),\n"
        "- dzienna kaloryczność ≈ USER_CTX.kcal ±10% + podsumowanie B/T/W dla każdego dnia,\n"
        "- lista zakupów ZBIORCZA zaokrąglona do opakowań,\n"
        "- wyklucz alergie/nielubiane; jeśli treats wliczane, rozlicz ich kcal i wskaż dni.\n"
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "system", "content": _ctx(profile or {})},
        {"role": "user", "content": f"FRAGMENTY:\n{_cap(snippets)}\n\nPROŚBA:\n{request}"},
    ]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2,
        max_tokens=1100,
    )
    return resp.choices[0].message.content.strip()

async def generate_workout(request: str, profile: dict | None = None, snippets: str = "") -> str:
    sys = (
        BASE_RULES +
        "Ułóż plan treningowy spójny z USER_CTX.\n"
        "Zasady:\n"
        "- liczba dni = wyciągnij z USER_CTX.training (np. ‘siłownia 3x’) lub z prośby; jeśli brak → 3 dni FBW\n"
        "- sen i stres modulują objętość: sen ≤6 h lub stres ‘wysoki’ → niższa objętość i RIR 2–3\n"
        "- priorytet ‘wygląd’ → hipertrofia: 8–12 powt., 10–20 serii tydz./partię, RIR 1–2, progresja liniowa\n"
        "- dom/siłownia dobierz z prośby; podaj zamienniki\n"
        "- format: Tydzień 1 (D1..Dn), Progresja (T1–T4), Zamienniki, Punkty techniczne\n"
        "Jeśli brak USER_CTX — poproś o /setup.\n"
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "system", "content": _ctx(profile or {})},
        {"role": "user", "content": f"FRAGMENTY:\n{_cap(snippets)}\n\nPROŚBA:\n{request}"},
    ]
    resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
    temperature=0.2,
    max_tokens=1100,
)
    return resp.choices[0].message.content.strip()