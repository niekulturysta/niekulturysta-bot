from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func

from db import Session, User, Checkin, Reminder
from .retrieval import search_by_kind
from .ai import generate_answer, generate_mealplan, generate_workout

print("NK handlers v2.0 loaded")
router = Router(name="handlers")

# ====== FSM Setup ======
class Setup(StatesGroup):
    goal = State()
    age = State()
    height = State()
    weight = State()
    activity = State()

class TrainingSetup(StatesGroup):
    days = State()
    time = State()
    equipment = State()
    level = State()
    priority = State()
    injuries = State()

class DietSetup(StatesGroup):
    meals = State()
    style = State()
    budget = State()
    cooking = State()
    allergies = State()
    dislikes = State()
    treats = State()

# ====== Metryki i kalkulatory ======
def _tdee_mifflin(sex: str, age: int, height_cm: int, weight_kg: float, activity: str) -> int:
    s = 5 if sex.lower().startswith("m") else -161
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + s
    mult = {"niska": 1.2, "średnia": 1.45, "srednia": 1.45, "wysoka": 1.7}.get(activity.lower(), 1.35)
    return int(round(bmr * mult))

def _target_kcal_for_goal(goal: str, tdee: int) -> int:
    g = (goal or "").lower()
    if g.startswith("redukc"):
        return int(round(tdee * 0.8))
    if g.startswith("masa"):
        return int(round(tdee * 1.15))
    return int(tdee)

def _macros_for_goal(goal: str, kcal: int, weight_kg: float) -> dict:
    g = (goal or "").lower()
    if g.startswith("redukc"):
        protein_g = int(round(weight_kg * 2.0))
        fat_g = int(round((kcal * 0.25) / 9))
    elif g.startswith("masa"):
        protein_g = int(round(weight_kg * 1.8))
        fat_g = int(round((kcal * 0.22) / 9))
    else:
        protein_g = int(round(weight_kg * 1.9))
        fat_g = int(round((kcal * 0.25) / 9))
    carbs_g = max(0, int(round((kcal - (protein_g * 4 + fat_g * 9)) / 4)))
    return {"protein_g": protein_g, "fat_g": fat_g, "carbs_g": carbs_g}

# ====== Migracja profilu i warstwy ======
def _ensure_layers(prof: dict) -> dict:
    """
    Migruje stary 'płaski' profil do warstw:
    - baseline: dane początkowe z /setup (nie zmieniamy)
    - current: stan bieżący (aktualna waga, tdee, kcal, makra)
    - policy: zasady korekt
    - pending_adjustment: oczekująca korekta po /raport (wymaga /akceptuj)
    """
    prof = (prof or {}).copy()

    # Domyślne policy
    default_policy = {
        "weekly_adjust_pct": 0.05,            # 5% kroku bazowego przy korektach
        "min_adjust_kcal": 100,
        "max_adjust_kcal": 300,
        "reduce_target_pct_range": [-0.010, -0.003],  # -1.0% do -0.3% / tydzień
        "bulk_target_pct_range": [0.0025, 0.005],     # +0.25% do +0.5% / tydzień
        "maint_tolerance": 0.002              # ±0.2% / tydzień
    }

    # Jeśli warstwy już istnieją, tylko dopełnij policy
    if "baseline" in prof and "current" in prof:
        pol = prof.get("policy") or {}
        for k, v in default_policy.items():
            pol.setdefault(k, v)
        prof["policy"] = pol
        # sanity: jeśli brakuje goal w baseline/current a istnieje stary klucz - uzupełnij
        if not prof["baseline"].get("goal") and prof.get("goal"):
            prof["baseline"]["goal"] = prof["goal"]
        if not prof["current"].get("goal") and prof.get("goal"):
            prof["current"]["goal"] = prof["goal"]
        return prof

    # Migracja z płaskiego
    goal = prof.get("goal")
    age = prof.get("age")
    height = prof.get("height")
    weight = prof.get("weight")
    activity = prof.get("activity") or "średnia"

    # fallbacki
    try:
        age = int(age) if age is not None else 30
    except: age = 30
    try:
        height = int(height) if height is not None else 175
    except: height = 175
    try:
        weight = float(weight) if weight is not None else 80.0
    except: weight = 80.0

    tdee_now = _tdee_mifflin("m", age, height, weight, activity)
    kcal_now = _target_kcal_for_goal(goal or "podtrzymanie", tdee_now)
    macros_now = _macros_for_goal(goal or "podtrzymanie", kcal_now, weight)

    prof2 = {
        "baseline": {
            "goal": goal or "podtrzymanie",
            "age": age,
            "height": height,
            "activity": activity,
            "weight_kg": weight,
        },
        "current": {
            "goal": goal or "podtrzymanie",
            "weight_kg": weight,
            "tdee": tdee_now,
            "kcal": kcal_now,
            "macros": macros_now,
        },
        "policy": default_policy,
        # zachowaj informacje żywieniowe / preferencje jeśli były
        "allergies": prof.get("allergies", ""),
        "dislikes": prof.get("dislikes", ""),
        "alcohol": prof.get("alcohol", "nie"),
        "sleep": prof.get("sleep", 7),
        "stress": prof.get("stress", "średni"),
        "training": prof.get("training", ""),
        # dla kompatybilności (stare klucze zostawiamy, ale current jest źródłem prawdy)
        "goal": goal or "podtrzymanie",
        "tdee": tdee_now,
        "kcal": kcal_now,
        "macros": macros_now,
    }
    return prof2

def _profile_of(user: User) -> dict:
    return (user.profile or {})

def _bias_query(q: str, prof: dict) -> str:
    # Bierzemy z current + preferencje
    cur = (prof.get("current") or {})
    parts = [
        q,
        f"cel:{cur.get('goal') or prof.get('goal','')}",
        f"kcal:{cur.get('kcal') or prof.get('kcal') or ''}",
        f"alergie:{prof.get('allergies','')}",
        f"dislikes:{prof.get('dislikes','')}",
        f"trening:{prof.get('training','')}",
    ]
    return " | ".join([p for p in parts if p])

def _soft_validate(ans: str, prof: dict) -> str:
    """
    Dodaje notki ostrzegawcze:
    - wykryte alergeny/nielubiane (z obsługą synonimów),
    - odjazd kcal względem celu (±10%).
    """
    low = ans.lower()
    flagged = []

    # Słowniczek synonimów dla najczęstszych grup
    ALLERGY_SYNONYMS = {
        "drób": ["kurczak", "indyk", "kurcz", "pierś z kurczaka", "udko", "skrzydełka", "filet z kurczaka", "indyka"],
        "grzyby": ["pieczarki", "borowik", "podgrzybek", "shiitake", "grzyb"],
        "nabiał": ["mleko", "ser", "jogurt", "twaróg", "mozzarella", "feta", "kefir", "maślanka"],
        "gluten": ["pszenica", "makaron", "chleb", "bułka", "tortilla", "mąka"],
        "orzechy": ["migdały", "arachidowe", "laskowe", "nerkowce", "orzech"],
        "ryby": ["łosoś", "dorsz", "tuńczyk", "makrela", "pstrąg", "śledź"],
    }

    def expand_tokens(raw: str) -> list[str]:
        toks = []
        for token in raw.split(","):
            t = token.strip().lower()
            if not t:
                continue
            toks.append(t)
            toks.extend(ALLERGY_SYNONYMS.get(t, []))
        return [x for x in toks if x]

    allergy_text = prof.get("allergies","")
    dislike_text = prof.get("dislikes","")
    tokens = expand_tokens(allergy_text) + expand_tokens(dislike_text)

    for t in set(tokens):
        if t and t in low:
            flagged.append(t)

    if flagged:
        ans += "\n\nUwaga: wykryto składniki do wykluczenia: " + ", ".join(sorted(set(flagged)))

    # Kaloryczność vs cel (±10%)
    try:
        import re
        kcal_target = float(
            (prof.get("current") or {}).get("kcal") or prof.get("kcal") or prof.get("tdee") or 0
        )
        if kcal_target:
            found = re.findall(r"(\d{3,4})\s*kcal", low)
            if found:
                nums = [int(x) for x in found]
                avg = sum(nums) / len(nums)
                lo, hi = kcal_target * 0.9, kcal_target * 1.1
                if not (lo <= avg <= hi):
                    ans += f"\n\nNotka: sprawdź kaloryczność vs Twój cel {int(kcal_target)} kcal (±10%)."
    except Exception:
        pass

    return ans

# ===POMOCNICZY POST-CHECK ===
def _guardrails_note(ans: str, prof: dict, intent_hint: str = "") -> str:
    try:
        low = ans.lower()
        # czas z profilu
        t = (prof.get("training_time") or (prof.get("current") or {}).get("training_time") or None)
        if t:
            try:
                t = int(t)
            except Exception:
                t = None

        # 1) FBW przy czasie ≤30 min → zasugeruj split
        if t and t <= 30 and ("fbw" in low or "full body" in low):
            ans += ("\n\nKorekta Niekulturysty: przy 30 min/sesję zamiast FBW wybierz split 2-grupowy "
                    "(Push/Pull lub Góra/Dół), 3–4 ćwiczenia łącznie, superserie/EMOM.")

        # 2) PPL przy czasie ≤30 min → zasugeruj Push/Pull lub Góra/Dół
        if t and t <= 30 and ("ppl" in low or "push/pull/legs" in low or "push pull legs" in low):
            ans += ("\n\nKorekta Niekulturysty: przy 30 min/sesję unikaj PPL. "
                    "Użyj splitu Push/Pull lub Góra/Dół z 3–4 ćwiczeniami łącznie.")

        # 3) Jadłospis 5–7 dni: wymuś ROTACJE + LISTĘ ZAKUPÓW (ZBIORCZA)
        # Traktujemy to szerzej: jeśli w pytaniu lub odpowiedzi występuje 'jadłospis' albo 'dzień 1..7',
        # a w treści brak 'rotacj' LUB brak 'lista zakupów' → dodaj notkę.
        mentions_days = any(tag in low for tag in ["dzień 1", "dzień 2", "dzień 3", "dzień 4", "dzień 5", "dzień 6", "dzień 7"])
        looks_like_mealplan = mentions_days or ("jadłospis" in low) or ("posiłk" in low)
        has_rotations = ("rotacj" in low) or ("śniadanie a" in low or "sniadanie a" in low)
        has_shopping = ("lista zakupów" in low) or ("zakupów (zbiorcza" in low) or ("zakupow (zbiorcza" in low)

        if looks_like_mealplan and (not has_rotations or not has_shopping):
            missing = []
            if not has_rotations: missing.append("ROTACJE A/B/C (śniadania), D/E (obiady), F/G (kolacje)")
            if not has_shopping: missing.append("LISTA ZAKUPÓW (ZBIORCZA) zaokrąglona do opakowań")
            ans += "\n\nUwaga: wprowadź " + " oraz ".join(missing) + "."

    except Exception:
        pass
    return ans


# === [NOWY HELPER] — korekta przy kontuzjach kręgosłupa/rwie ===
def _injury_guardrails_note(ans: str, prof: dict) -> str:
    try:
        low_ans = ans.lower()
        injuries = (prof.get("injuries") or prof.get("current", {}).get("injuries") or "").lower()
        if not injuries:
            return ans

        spine_flags = any(k in injuries for k in ["rwa", "kręgosłup", "kregoslup", "lędźwi", "ledzwi"])
        if not spine_flags:
            return ans

        risky = ["martwy ciąg", "przysiad", "good morning", "dzień dobry", "back squat", "deadlift"]
        if any(r in low_ans for r in risky):
            ans += (
                "\n\nKorekta Niekulturysty (bezpieczeństwo kręgosłupa): "
                "unikaj ciężkiej osiowej kompresji (klasyczny martwy ciąg, przysiad tylni, good morning). "
                "Zamienniki: trap bar deadlift lub RDL z umiarkowanym ciężarem, hip thrust, Bulgarian split squat, "
                "leg press/hack squat; wiosłowania na maszynach/wyciągach. Trzymaj RIR 2–3 i kontrolowane tempo."
            )
    except Exception:
        pass
    return ans

def _context_footer(prof: dict) -> str:
    goal = prof.get("goal", "—")
    kcal = (prof.get("current") or {}).get("kcal") or prof.get("kcal") or prof.get("tdee") or "—"
    mpd  = prof.get("meals_per_day", "—")
    style = prof.get("diet_style", "—")
    t_days = prof.get("training_days", "—")
    t_time = prof.get("training_time", "—")
    eq = prof.get("training_equipment", "—")
    inj = prof.get("injuries", "brak")
    return (
        "\n\n—\n"
        f"_Użyty kontekst:_ cel: **{goal}**, kcal: **{kcal}**; dieta: **{mpd} pos./d, {style}**; "
        f"trening: **{t_days}×/{t_time} min, {eq}**; kontuzje: **{inj}**."
    )



# ====== /setup (nadpisuje, ustawia baseline i current) ======
@router.message(Command("setup"))
async def setup_start(m: Message, state: FSMContext):
    await m.answer("Podaj swój cel: redukcja, podtrzymanie, masa")
    await state.set_state(Setup.goal)

@router.message(Setup.goal)
async def setup_goal(m: Message, state: FSMContext):
    await state.update_data(goal=m.text.strip().lower())
    await m.answer("Podaj swój wiek (lata):")
    await state.set_state(Setup.age)

@router.message(Setup.age)
async def setup_age(m: Message, state: FSMContext):
    try:
        age = int(m.text.strip())
    except ValueError:
        await m.answer("Wpisz poprawny wiek jako liczbę.")
        return
    await state.update_data(age=age)
    await m.answer("Podaj swój wzrost (cm):")
    await state.set_state(Setup.height)

@router.message(Setup.height)
async def setup_height(m: Message, state: FSMContext):
    try:
        height = int(m.text.strip())
    except ValueError:
        await m.answer("Wpisz poprawny wzrost jako liczbę w cm.")
        return
    await state.update_data(height=height)
    await m.answer("Podaj swoją wagę (kg):")
    await state.set_state(Setup.weight)

@router.message(Setup.weight)
async def setup_weight(m: Message, state: FSMContext):
    try:
        weight = float(m.text.strip().replace(",", "."))
    except ValueError:
        await m.answer("Wpisz poprawną wagę w kg.")
        return
    await state.update_data(weight=weight)
    await m.answer("Podaj poziom aktywności: niska, średnia, wysoka")
    await state.set_state(Setup.activity)

@router.message(Setup.activity)
async def setup_activity(m: Message, state: FSMContext):
    activity = m.text.strip().lower()
    await state.update_data(activity=activity)

    data = await state.get_data()
    goal, age, height, weight, activity = (
        data["goal"], data["age"], data["height"], data["weight"], data["activity"]
    )

    tdee = _tdee_mifflin("m", age, height, weight, activity)
    kcal = _target_kcal_for_goal(goal, tdee)
    macros = _macros_for_goal(goal, kcal, weight)

    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            user = User(
                tg_id=m.from_user.id,
                username=m.from_user.username,
                first_name=m.from_user.first_name,
                last_name=m.from_user.last_name,
                profile={},
            )
            s.add(user)
            await s.commit()

        # Reset i zapis warstw
        prof = {
            "baseline": {
                "goal": goal,
                "age": age,
                "height": height,
                "activity": activity,
                "weight_kg": weight,
            },
            "current": {
                "goal": goal,
                "weight_kg": weight,
                "tdee": tdee,
                "kcal": kcal,
                "macros": macros,
            },
            "policy": {
                "weekly_adjust_pct": 0.05,
                "min_adjust_kcal": 100,
                "max_adjust_kcal": 300,
                "reduce_target_pct_range": [-0.010, -0.003],
                "bulk_target_pct_range": [0.0025, 0.005],
                "maint_tolerance": 0.002,
            },
            "allergies": "",
            "dislikes": "",
            "alcohol": "nie",
            "sleep": 7,
            "stress": "średni",
            "training": "",
            # kompatybilność
            "goal": goal,
            "tdee": tdee,
            "kcal": kcal,
            "macros": macros,
        }
        user.profile = prof
        await s.commit()
        await s.refresh(user)

    await m.answer(
        f"✅ Profil zapisany.\n"
        f"Cel: {goal}, kcal: {kcal}\n"
        f"Makra → B: {macros['protein_g']} g • T: {macros['fat_g']} g • W: {macros['carbs_g']} g\n"
        "(Poprzednie dane zostały nadpisane.)"
    )
    await state.clear()

# ====== START / BASIC ======
@router.message(Command("start"))
async def cmd_start(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            user = User(
                tg_id=m.from_user.id,
                username=m.from_user.username,
                first_name=m.from_user.first_name,
                last_name=m.from_user.last_name,
                profile={},
            )
            s.add(user); await s.commit()
        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()
    cur = prof.get("current") or {}
    goal = cur.get("goal") or prof.get("goal") or "—"
    kcal = cur.get("kcal") or cur.get("tdee") or "—"
    await m.answer(
        "Jestem Niekulturysta AI 💪\n"
        "Komendy: /setup, /trening-setup, /jadlospis-setup, /kcal, /checkin, /raport, /akceptuj, /cofnij, /ask, /plan, /jadlospis, /trening, /powiadomienia\n"
        f"Twój cel: {goal}, kcal: {kcal}"
    )

@router.message(Command("debug"))
async def cmd_debug(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
    await m.answer(f"profile = {user.profile if user else None}")

# ====== PLAN / ASK ======
@router.message(Command("plan"))
async def cmd_plan(m: Message):
    q = m.text.replace("/plan", "", 1).strip() or "Plan startowy zgodny z moim profilem"
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()
        bias = _bias_query(q, prof)
        hits = []
        hits += await search_by_kind(s, bias, "note",  k=4)
        hits += await search_by_kind(s, bias, "ebook", k=2)
        hits += await search_by_kind(s, bias, "study", k=1)
    snippets = "\n\n---\n".join([f"{t}\n{c}" for (t, c, _m) in hits]) if hits else ""
    ans = await generate_answer(q, profile=prof, snippets=snippets)
    ans = _soft_validate(ans, prof)
    ans = _guardrails_note(ans, prof, intent_hint="ask")
    ans = _injury_guardrails_note(ans, prof)
    


    await m.answer(ans)

@router.message(Command("ask"))
async def cmd_ask(m: Message):
    q = m.text.replace("/ask", "", 1).strip()
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user or not user.profile:
            await m.answer("Najpierw zrób /setup.")
            return
        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()

    if not q:
        cur = prof.get("current") or {}
        kcal = cur.get("kcal") or cur.get("tdee") or "—"
        goal = cur.get("goal") or prof.get("goal") or "—"
    await m.answer(
        f"Napisz pytanie po /ask …\n"
        "Przykłady:\n"
        "• /ask trening 3 dni, dom, hantle, priorytet barki\n"
        "• /ask jadłospis 3 dni, szybkie i tanie, bez nabiału\n"
        f"• /ask jak rozkładać białko przy {kcal} kcal na {goal}?\n"
        "• /ask mam 30 min dziennie, co wybrać FBW czy PPL?\n"
    )
    return

    # Retrieval (Twoje materiały) + odpowiedź
    async with Session() as s:
        bias = _bias_query(q, prof)
        hits = []
        hits += await search_by_kind(s, bias, "note",  k=4)
        hits += await search_by_kind(s, bias, "ebook", k=2)
        hits += await search_by_kind(s, bias, "study", k=1)
        snippets = "\n\n---\n".join([f"{t}\n{c}" for (t, c, _m) in hits]) if hits else ""
    ans = await generate_answer(q, profile=prof, snippets=snippets)
    ans = _soft_validate(ans, prof)
    ans = _guardrails_note(ans, prof, intent_hint="ask")
    ans = _injury_guardrails_note(ans, prof)
    ans += _context_footer(prof)

    await m.answer(ans)

# ====== JADŁOSPIS / TRENING ======
@router.message(Command("jadlospis"))
async def cmd_jadlospis(m: Message):
    req = m.text.replace("/jadlospis", "", 1).strip() or "plan na 3 dni, 3 posiłki/dzień, szybkie i tanie"
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()
        bias = _bias_query(req, prof)
        hits = []
        hits += await search_by_kind(s, bias, "note",  k=3)
        hits += await search_by_kind(s, bias, "ebook", k=2)
    snippets = "\n\n---\n".join([f"{t}\n{c}" for (t, c, _m) in hits]) if hits else ""
    ans = await generate_mealplan(req, profile=prof, snippets=snippets)
    ans = _soft_validate(ans, prof)
    ans = _guardrails_note(ans, prof, intent_hint="jadlospis")
    ans = _injury_guardrails_note(ans, prof)
    ans += _context_footer(prof)

    await m.answer(ans)

@router.message(Command("trening"))
async def cmd_trening(m: Message):
    req = m.text.replace("/trening", "", 1).strip() or "4 dni, siłownia, priorytet barki"
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()
        bias = _bias_query(req, prof)
        hits = []
        hits += await search_by_kind(s, bias, "note",  k=3)
        hits += await search_by_kind(s, bias, "ebook", k=2)
    snippets = "\n\n---\n".join([f"{t}\n{c}" for (t, c, _m) in hits]) if hits else ""
    
    ans = await generate_workout(req, profile=prof, snippets=snippets)
    ans = _soft_validate(ans, prof)
    ans = _guardrails_note(ans, prof, intent_hint="trening")
    ans = _injury_guardrails_note(ans, prof)
    ans += _context_footer(prof)

    await m.answer(ans)

# ====== CHECKIN / RAPORT / AKCEPTUJ / COFNIJ / KCAL / POWIAD ======
@router.message(Command("checkin"))
async def cmd_checkin(m: Message):
    args = m.text.split(maxsplit=2)
    weight = None
    note = None
    if len(args) >= 2:
        try:
            weight = float(args[1].replace(",", "."))
        except ValueError:
            pass
    if len(args) == 3:
        note = args[2].strip().strip('"\'')
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start.")
            return
        chk = Checkin(user_id=user.id, weight_kg=weight, note=note)
        s.add(chk); await s.commit()
        cnt = (await s.execute(select(func.count(Checkin.id)).where(Checkin.user_id == user.id))).scalar_one()
    await m.answer(f"Zapisane ✅\nWpisów łącznie: {cnt}")

@router.message(Command("raport"))
async def cmd_raport(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start."); return

        prof = _ensure_layers(_profile_of(user))
        if prof != user.profile:
            user.profile = prof
            await s.commit()

        # pobierz ostatnie 14 checkinów
        checks = (await s.execute(
            select(Checkin).where(Checkin.user_id == user.id).order_by(Checkin.created_at.desc()).limit(14)
        )).scalars().all()

        if not checks:
            await m.answer('Brak checkinów. Użyj /checkin 100.2 "komentarz".'); return

        weights = [c.weight_kg for c in checks if c.weight_kg is not None]
        if not weights:
            await m.answer("Brak wagi w ostatnich checkinach."); return

        # średnie 7-dniowe (rolling)
        recent = weights[:7]
        prev = weights[7:14]
        avg_recent = sum(recent)/len(recent)
        line = f"Średnia 7d: {avg_recent:.1f} kg"
        delta_txt = ""
        weekly_rate = None
        if prev:
            avg_prev = sum(prev)/len(prev)
            delta = avg_recent - avg_prev
            weekly_rate = delta / avg_prev if avg_prev else 0.0
            delta_txt = f" (zmiana vs poprzedni tydz.: {delta:+.1f} kg, {weekly_rate:+.2%}/tydz.)"
            line += delta_txt

        # Decyzja o korekcie
        cur = prof.get("current") or {}
        base = prof.get("baseline") or {}
        pol = prof.get("policy") or {}

        goal = cur.get("goal") or base.get("goal") or "podtrzymanie"
        age = base.get("age", 30)
        height = base.get("height", 175)
        activity = base.get("activity", "średnia")

        # Bez korekty jeśli zbyt mało danych
        if weekly_rate is None or len(recent) < 4:
            await m.answer(line + "\nZa mało danych do korekty (potrzeba >= 4 ważenia w tygodniu).")
            return

        # wylicz TDEE na aktualnej masie (avg_recent)
        tdee_now = _tdee_mifflin("m", age, height, avg_recent, activity)
        kcal_now = int(cur.get("kcal") or _target_kcal_for_goal(goal, tdee_now))

        decision = "bez zmian"
        apply_delta = 0
        reason = ""

        if goal.startswith("redukc"):
            lo, hi = pol.get("reduce_target_pct_range", [-0.010, -0.003])
            if weekly_rate > hi:  # spadek za wolny (mniej ujemny)
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * pol.get("weekly_adjust_pct",0.05)))))
                apply_delta = -step
                decision = "obniżka kcal"
                reason = f"spadek za wolny ({weekly_rate:+.2%}/tydz., cel {lo:.1%}..{hi:.1%})"
            elif weekly_rate < lo:  # spadek za szybki
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * pol.get("weekly_adjust_pct",0.05)))))
                apply_delta = +step
                decision = "podwyżka kcal"
                reason = f"spadek za szybki ({weekly_rate:+.2%}/tydz., cel {lo:.1%}..{hi:.1%})"
        elif goal.startswith("masa"):
            lo, hi = pol.get("bulk_target_pct_range", [0.0025, 0.005])
            if weekly_rate < lo:  # przyrost za wolny
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * pol.get("weekly_adjust_pct",0.05)))))
                apply_delta = +step
                decision = "podwyżka kcal"
                reason = f"przyrost za wolny ({weekly_rate:+.2%}/tydz., cel {lo:.2%}..{hi:.2%})"
            elif weekly_rate > hi:  # przyrost za szybki
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * pol.get("weekly_adjust_pct",0.05)))))
                apply_delta = -step
                decision = "obniżka kcal"
                reason = f"przyrost za szybki ({weekly_rate:+.2%}/tydz., cel {lo:.2%}..{hi:.2%})"
        else:
            tol = pol.get("maint_tolerance", 0.002)
            if weekly_rate > tol:
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * 0.03))))
                apply_delta = -step
                decision = "obniżka kcal (utrzymanie)"
                reason = f"masa rośnie {weekly_rate:+.2%}/tydz. (> {tol:.2%})"
            elif weekly_rate < -tol:
                step = max(pol.get("min_adjust_kcal",100), min(pol.get("max_adjust_kcal",300), int(abs(kcal_now * 0.03))))
                apply_delta = +step
                decision = "podwyżka kcal (utrzymanie)"
                reason = f"masa spada {weekly_rate:+.2%}/tydz. (< -{tol:.2%})"

        # Zapisz pending_adjustment i zaproponuj
        new_kcal = kcal_now + apply_delta
        # przelicz makra na aktualnej wadze
        new_macros = _macros_for_goal(goal, new_kcal, avg_recent)

        text = line + "\n"
        text += f"Cel: {goal}\nAktualna waga (7d): {avg_recent:.1f} kg\n"
        text += f"Bieżące cele: {kcal_now} kcal | B {cur.get('macros',{}).get('protein_g','-')} g • T {cur.get('macros',{}).get('fat_g','-')} g • W {cur.get('macros',{}).get('carbs_g','-')} g\n"

        if apply_delta == 0:
            # Aktualizujemy current (waga, tdee) bez zmiany kcal
            prof["current"]["weight_kg"] = avg_recent
            prof["current"]["tdee"] = tdee_now
            user.profile = prof
            await s.commit()
            await m.answer(text + "Tempo w normie → bez zmian kcal.\nTip: pilnuj 7–8 h snu i stałej pory ważenia.")
            return

        # zapisz pending
        prof["pending_adjustment"] = {
            "proposed_at": "now",  # prosty znacznik; w realu można dać isoformat czasu
            "from_kcal": kcal_now,
            "to_kcal": new_kcal,
            "delta": apply_delta,
            "reason": reason,
            "ref_weight_kg": avg_recent,
            "ref_tdee": tdee_now,
            "goal": goal,
            "macros": new_macros,
        }
        # aktualizuj current/waga/tdee
        prof["current"]["weight_kg"] = avg_recent
        prof["current"]["tdee"] = tdee_now

        user.profile = prof
        await s.commit()

        sign = "+" if apply_delta > 0 else ""
        text += f"Propozycja: {decision} {sign}{apply_delta} kcal → **{new_kcal} kcal**\n"
        text += f"Nowe makra: B {new_macros['protein_g']} g • T {new_macros['fat_g']} g • W {new_macros['carbs_g']} g\n"
        text += "Aby zastosować: wpisz /akceptuj\nAby odrzucić: wpisz /cofnij"
        await m.answer(text)

@router.message(Command("akceptuj"))
async def cmd_accept(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start."); return
        prof = _ensure_layers(_profile_of(user))
        pending = prof.get("pending_adjustment")
        if not pending:
            await m.answer("Brak oczekującej korekty."); return

        cur = prof.get("current") or {}
        # zachowaj poprzednią wartość aby móc cofnąć
        prof["last_applied_kcal"] = cur.get("kcal")
        prof["current"]["kcal"] = int(pending["to_kcal"])
        prof["current"]["macros"] = pending["macros"]
        # doczyszczamy pending
        prof["pending_adjustment"] = None

        user.profile = prof
        await s.commit()
    await m.answer("✅ Zastosowano nową kaloryczność i makra.")

@router.message(Command("cofnij"))
async def cmd_revert(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start."); return
        prof = _ensure_layers(_profile_of(user))
        last = prof.get("last_applied_kcal")
        if last is None:
            await m.answer("Brak poprzedniej wartości do przywrócenia."); return

        cur = prof.get("current") or {}
        # przelicz makra po przywróceniu kcal bazując na bieżącej wadze
        weight = float(cur.get("weight_kg") or prof.get("baseline",{}).get("weight_kg") or 80.0)
        goal = cur.get("goal") or prof.get("baseline",{}).get("goal") or "podtrzymanie"
        prof["current"]["kcal"] = int(last)
        prof["current"]["macros"] = _macros_for_goal(goal, int(last), weight)

        # wyczyść last_applied_kcal po cofnięciu
        prof["last_applied_kcal"] = None

        user.profile = prof
        await s.commit()
    await m.answer("↩️ Przywrócono poprzednią kaloryczność i makra.")

@router.message(Command("kcal"))
async def cmd_kcal(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
    if not user or not user.profile:
        await m.answer("Najpierw zrób /setup."); return
    prof = _ensure_layers(user.profile)
    cur = prof.get("current") or {}
    kcal = cur.get("kcal") or cur.get("tdee") or "—"
    macros = cur.get("macros", {})
    await m.answer(
        f"Aktualnie: {kcal} kcal (TDEE ~ {cur.get('tdee','—')})\n"
        f"B: {macros.get('protein_g','-')} g • T: {macros.get('fat_g','-')} g • W: {macros.get('carbs_g','-')} g"
    )

@router.message(Command("powiadomienia"))
async def cmd_powiad(m: Message):
    args = m.text.split(maxsplit=2)
    if len(args) < 3:
        await m.answer('Użycie: /powiadomienia 20:00 "treść przypomnienia"'); return
    time_s = args[1]; text = args[2].strip().strip('"\'')
    hh, mm = map(int, time_s.split(":"))
    from datetime import datetime, timedelta, time as dtime, timezone
    now = datetime.now(timezone.utc)
    target = datetime.combine(now.date(), dtime(hh, mm, tzinfo=timezone.utc))
    if target <= now: target = target + timedelta(days=1)
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user: await m.answer("Użyj najpierw /start."); return
        rem = Reminder(user_id=user.id, chat_id=m.chat.id, text=text, next_run_at=target, active=True)
        s.add(rem); await s.commit()
    await m.answer(f"Ustawiono przypomnienie na {time_s} (UTC).")

# === [WSTAWKA 2] /trening-setup (FSM) ===
@router.message(Command("trening-setup"))
async def trening_setup_start(m: Message, state: FSMContext):
    await m.answer("Podaj liczbę dni treningowych w tygodniu (2–5):")
    await state.set_state(TrainingSetup.days)

@router.message(TrainingSetup.days)
async def trening_setup_days(m: Message, state: FSMContext):
    try:
        d = int(m.text.strip())
        if d < 2 or d > 5:
            raise ValueError
    except ValueError:
        await m.answer("Wpisz liczbę z zakresu 2–5.")
        return
    await state.update_data(days=d)
    await m.answer("Ile minut trwa Twoja typowa sesja? (30 / 45 / 60 / 75):")
    await state.set_state(TrainingSetup.time)

@router.message(TrainingSetup.time)
async def trening_setup_time(m: Message, state: FSMContext):
    try:
        t = int(m.text.strip())
        if t not in (30, 45, 60, 75):
            raise ValueError
    except ValueError:
        await m.answer("Wpisz jedną z wartości: 30, 45, 60 lub 75.")
        return
    await state.update_data(time=t)
    await m.answer("Gdzie trenujesz? (dom / siłownia / hantle / gumy):")
    await state.set_state(TrainingSetup.equipment)

@router.message(TrainingSetup.equipment)
async def trening_setup_equipment(m: Message, state: FSMContext):
    eq = m.text.strip().lower()
    allowed = {"dom","siłownia","silownia","hantle","gumy"}
    if eq not in allowed:
        await m.answer("Wpisz jedną z opcji: dom / siłownia / hantle / gumy.")
        return
    if eq == "silownia":  # korekta bez polskich znaków
        eq = "siłownia"
    await state.update_data(equipment=eq)
    await m.answer("Jaki masz poziom zaawansowania? (start / średnio / zaawansowany):")
    await state.set_state(TrainingSetup.level)

@router.message(TrainingSetup.level)
async def trening_setup_level(m: Message, state: FSMContext):
    lvl = m.text.strip().lower()
    allowed = {"start","średnio","srednio","zaawansowany"}
    if lvl not in allowed:
        await m.answer("Wpisz: start / średnio / zaawansowany.")
        return
    if lvl == "srednio":
        lvl = "średnio"
    await state.update_data(level=lvl)
    await m.answer("Na jaką partię chcesz położyć priorytet? (barki / plecy / klata / nogi / brzuch / ramiona):")
    await state.set_state(TrainingSetup.priority)

@router.message(TrainingSetup.priority)
async def trening_setup_priority(m: Message, state: FSMContext):
    pr = m.text.strip().lower()
    allowed = {"barki","plecy","klata","nogi","brzuch","ramiona"}
    if pr not in allowed:
        await m.answer("Wpisz jedną z opcji: barki / plecy / klata / nogi / brzuch / ramiona.")
        return
    await state.update_data(priority=pr)
    await m.answer("Masz jakieś kontuzje lub ograniczenia? Jeśli nie, napisz „brak”.")
    await state.set_state(TrainingSetup.injuries)

@router.message(TrainingSetup.injuries)
async def trening_setup_injuries(m: Message, state: FSMContext):
    inj = m.text.strip()
    data = await state.get_data()
    days = data["days"]; time = data["time"]; eq = data["equipment"]
    lvl = data["level"]; pr = data["priority"]

    # zapis do profilu
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start.")
            await state.clear()
            return

        # zapewnij warstwy zgodnie z nową architekturą
        prof = _ensure_layers(user.profile or {})
        # zapisujemy szczegóły treningu w profilu (klucze jawne + zwięzłe podsumowanie dla kompatybilności)
        prof["training_days"] = days
        prof["training_time"] = time
        prof["training_equipment"] = eq
        prof["training_level"] = lvl
        prof["training_priority"] = pr
        prof["injuries"] = inj or "brak"
        prof["training"] = f"{days}x/{time}min/{eq}/prio:{pr}/lvl:{lvl}"

        user.profile = prof
        await s.commit()

    await m.answer(
        "✅ Profil treningowy zapisany.\n"
        f"Dni/tydz.: {days} • Czas: {time} min • Sprzęt: {eq}\n"
        f"Poziom: {lvl} • Priorytet: {pr} • Kontuzje: {inj or 'brak'}"
    )
    await state.clear()

# === [WSTAWKA 2] /jadlospis-setup (FSM) ===
@router.message(Command("jadlospis-setup"))
async def diet_setup_start(m: Message, state: FSMContext):
    await m.answer("Ile posiłków dziennie preferujesz? (2 / 3 / 4 / 5):")
    await state.set_state(DietSetup.meals)

@router.message(DietSetup.meals)
async def diet_setup_meals(m: Message, state: FSMContext):
    try:
        n = int(m.text.strip())
        if n not in (2, 3, 4, 5):
            raise ValueError
    except ValueError:
        await m.answer("Wpisz liczbę z zestawu: 2, 3, 4 lub 5.")
        return
    await state.update_data(meals=n)
    await m.answer("Jaki styl jedzenia? (klasyczna / śródziemno / wege / keto / high-protein / low-carb):")
    await state.set_state(DietSetup.style)

@router.message(DietSetup.style)
async def diet_setup_style(m: Message, state: FSMContext):
    s = m.text.strip().lower()
    allowed = {"klasyczna","śródziemno","srodziemno","wege","keto","high-protein","low-carb"}
    if s not in allowed:
        await m.answer("Wpisz jedną z opcji: klasyczna / śródziemno / wege / keto / high-protein / low-carb.")
        return
    if s == "srodziemno":
        s = "śródziemno"
    await state.update_data(style=s)
    await m.answer("Budżet na jedzenie? (niski / średni / wysoki):")
    await state.set_state(DietSetup.budget)

@router.message(DietSetup.budget)
async def diet_setup_budget(m: Message, state: FSMContext):
    b = m.text.strip().lower()
    allowed = {"niski","średni","sredni","wysoki"}
    if b not in allowed:
        await m.answer("Wpisz: niski / średni / wysoki.")
        return
    if b == "sredni":
        b = "średni"
    await state.update_data(budget=b)
    await m.answer("Ile czasu chcesz poświęcać na gotowanie? (szybko 10–15 / średnio 20–30 / dłużej 30–45):")
    await state.set_state(DietSetup.cooking)

@router.message(DietSetup.cooking)
async def diet_setup_cooking(m: Message, state: FSMContext):
    txt = m.text.strip().lower()
    # akceptujemy skróty: szybko/średnio/dłużej
    if "szyb" in txt:
        c = "szybko 10–15"
    elif "śred" in txt or "sred" in txt:
        c = "średnio 20–30"
    elif "dłuż" in txt or "dluz" in txt or "30–45" in txt or "30-45" in txt:
        c = "dłużej 30–45"
    else:
        await m.answer("Wpisz: szybko 10–15 / średnio 20–30 / dłużej 30–45.")
        return
    await state.update_data(cooking=c)
    await m.answer("Alergie (wypisz po przecinku) — jeśli brak, napisz „brak”.")
    await state.set_state(DietSetup.allergies)

@router.message(DietSetup.allergies)
async def diet_setup_allergies(m: Message, state: FSMContext):
    allg = m.text.strip()
    await state.update_data(allergies=allg)
    await m.answer("Czego nie lubisz (po przecinku) — jeśli nic, napisz „brak”.")
    await state.set_state(DietSetup.dislikes)

@router.message(DietSetup.dislikes)
async def diet_setup_dislikes(m: Message, state: FSMContext):
    dis = m.text.strip()
    await state.update_data(dislikes=dis)
    await m.answer("Treats (alkohol/słodycze) wliczać w kalorie? (wliczaj / okazjonalnie / nie):")
    await state.set_state(DietSetup.treats)

@router.message(DietSetup.treats)
async def diet_setup_treats(m: Message, state: FSMContext):
    tr = m.text.strip().lower()
    allowed = {"wliczaj","okazjonalnie","nie"}
    if tr not in allowed:
        await m.answer("Wpisz: wliczaj / okazjonalnie / nie.")
        return

    data = await state.get_data()
    meals = data["meals"]; style = data["style"]; budget = data["budget"]
    cooking = data["cooking"]; allergies = data["allergies"]; dislikes = data["dislikes"]

    # zapis do profilu
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await m.answer("Użyj najpierw /start.")
            await state.clear()
            return

        prof = _ensure_layers(user.profile or {})
        prof["meals_per_day"] = meals
        prof["diet_style"] = style
        prof["budget"] = budget
        prof["cooking_time"] = cooking
        prof["allergies"] = allergies
        prof["dislikes"] = dislikes
        prof["treats_policy"] = tr
        # podsumowanie skrótowe (dla RAG/AI)
        prof["diet_pref"] = f"{meals} posiłki/d • {style} • {budget} • {cooking} • treats:{tr}"

        user.profile = prof
        await s.commit()

    await m.answer(
        "✅ Profil dietetyczny zapisany.\n"
        f"Posiłki/dzień: {meals} • Styl: {style} • Budżet: {budget}\n"
        f"Gotowanie: {cooking}\n"
        f"Alergie: {allergies or 'brak'} • Nielubiane: {dislikes or 'brak'} • Treats: {tr}"
    )
    await state.clear()



# ====== Fallback HELP ======
@router.message(F.text & ~F.text.startswith("/"))
async def any_text(m: Message):
    async with Session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        prof = _ensure_layers(_profile_of(user)) if user else {}
        if user and prof != user.profile:
            user.profile = prof
            await s.commit()
    cur = (prof.get("current") or {}) if prof else {}
    goal = cur.get("goal") or prof.get("goal") if prof else "—"
    kcal = cur.get("kcal") or cur.get("tdee") if prof else "—"
    await m.answer(
        "Jestem Niekulturysta AI 💪\n"
        "Komendy: /setup, /kcal, /checkin, /raport, /akceptuj, /cofnij, /ask, /plan, /jadlospis, /trening, /powiadomienia\n"
        f"Twój cel: {goal or '—'}, kcal: {kcal or '—'}"
    )
