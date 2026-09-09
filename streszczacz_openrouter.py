# -*- coding: utf-8 -*-
"""
streszczacz_openrouter.py — Klient OpenRouter do streszczania interpretacji.
NIEZALEŻNY od Streamlit (można go użyć też w skrypcie GitHub Actions).

API OpenRouter jest zgodne z OpenAI: POST na /chat/completions z nagłówkiem
Authorization: Bearer <klucz>. Domyślny model to auto-router "openrouter/free",
który sam dobiera dostępny darmowy model — odporny na rotację darmowej oferty.

Klucz API przekazuje się z zewnątrz (z st.secrets w Streamlit albo z os.environ
w GitHub Actions) — ten moduł go nie czyta samodzielnie.

TAKSONOMIA — ŹRÓDŁO PRAWDY W BAZIE
  Listy BRANZE i PRZEDMIOTY były wcześniej zaszyte w tym pliku i utrzymywane
  ręcznie także w pliku wiedzy GPT dla modułu 5. Wtyczka przeglądarkowa byłaby
  trzecią kopią, w dodatku rozsianą po dyskach pracowników — i pierwsza zmiana
  taksonomii rozjechałaby je po cichu.

  Teraz źródłem prawdy są tabele taksonomia_branze i taksonomia_przedmiotow.
  Stałe poniżej ZOSTAJĄ jako awaryjny fallback: gdy baza nie odpowie, moduł
  działa dokładnie tak jak przed zmianą. To celowe — automat streszczający nie
  może przestać działać dlatego, że tabela słownikowa jest chwilowo niedostępna.

  Odczyt jest leniwy (przy pierwszym użyciu) i buforowany. Dzięki temu wszyscy
  odbiorcy — automat, moduł 6, Edge Functions wtyczki — widzą tę samą listę,
  bez zmian w kodzie wywołującym.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Auto-router darmowych modeli — przeżywa rotację (gdy konkretny :free znika).
MODEL_DOMYSLNY = "openrouter/free"

MODELE_DO_WYBORU = [
    "openrouter/free",
    "meta-llama/llama-4-maverick:free",
    "meta-llama/llama-4-scout:free",
    "qwen/qwen3-235b-a22b:free",
    "openai/gpt-oss-120b:free",
]

# Wystarczająco dużo, by 12-zdaniowe polskie streszczenie NIE zostało ucięte
# (polski jest „cięższy” tokenowo niż angielski — przy 1200 tokenach odpowiedzi
#  bywały urywane w połowie zdania, co psuło JSON).
MAKS_TOKENOW = 2200

# Zamknięta taksonomia branż — model MUSI wybierać z tej listy (swobodne
# nazewnictwo uniemożliwiłoby dopasowanie subskrypcji: raz „ciepłownictwo”,
# raz „branża grzewcza”). Modyfikując listę pamiętaj, że subskrypcje w bazie
# odwołują się do tych dokładnych wartości.
#
# UWAGA: to jest już tylko FALLBACK. Wartości obowiązujące czytane są z tabeli
# taksonomia_branze — patrz sekcja „TAKSONOMIA Z BAZY” niżej. Zmiana tutaj nie
# wpłynie na działanie, dopóki baza odpowiada; żeby przenieść zmianę do bazy,
# uruchom zasil_taksonomie.py.
BRANZE = [
    "ciepłownicza",
    "wodno-kanalizacyjna",
    "energetyczna",
    "OZE i fotowoltaika",
    "budowlana i deweloperska",
    "nieruchomości",
    "transportowa i logistyczna",
    "motoryzacyjna",
    "IT i telekomunikacja",
    "finansowa i ubezpieczeniowa",
    "medyczna i farmaceutyczna",
    "rolnicza i spożywcza",
    "handlowa",
    "produkcyjna",
    "gastronomiczna i hotelarska",
    "edukacyjna",
    "samorządowa (JST)",
    "inna",
]


# Taksonomie PRZEDMIOTU interpretacji (obszar merytoryczny) — osobna lista
# per podatek. Model wybiera JEDEN dominujący przedmiot z listy właściwej dla
# podatku danej interpretacji. Ostatnia pozycja każdej listy to bezpiecznik
# "inne...".
#
# UWAGA: to jest już tylko FALLBACK — obowiązuje tabela taksonomia_przedmiotow.
# Kolejność ma znaczenie merytoryczne: walidacja zwraca PIERWSZE trafienie,
# a model dostaje listę w prompcie w tej samej kolejności. Dlatego tabela
# przechowuje kolumnę `kolejnosc`.
PRZEDMIOTY = {
    "CIT": [
        "estoński CIT (ryczałt od dochodów spółek)", "podatek u źródła (WHT)",
        "ceny transferowe i podmioty powiązane", "koszty finansowania dłużniczego",
        "koszty uzyskania przychodów — zasady ogólne", "moment potrącalności kosztów",
        "amortyzacja i środki trwałe", "przychody podatkowe — moment i rozpoznanie",
        "różnice kursowe", "ulga B+R i IP Box", "pozostałe ulgi i odliczenia",
        "zwolnienia podatkowe (w tym strefowe/PSI)",
        "reorganizacje (połączenia, podziały, aport, wymiana udziałów)",
        "dywidendy i przychody z udziału w zyskach", "rozliczenia w grupie (PGK)",
        "minimalny podatek dochodowy i podatek od przychodów z budynków",
        "ograniczenia w kosztach (usługi niematerialne)",
        "podatek od przerzuconych dochodów", "zagraniczna jednostka kontrolowana (CFC)",
        "rezydencja podatkowa i zakład (PE)", "świadczenia nieodpłatne",
        "wierzytelności i ulga na złe długi", "leasing", "fundacja rodzinna (CIT)",
        "obowiązki dokumentacyjne (MDR, TPR, CbC)", "inne zagadnienia CIT",
    ],
    "VAT": [
        "stawki VAT", "zwolnienia przedmiotowe", "zwolnienie podmiotowe (limit)",
        "prawo do odliczenia i proporcja/prewspółczynnik",
        "moment powstania obowiązku podatkowego", "podstawa opodatkowania",
        "wewnątrzwspólnotowa dostawa towarów (WDT)",
        "wewnątrzwspólnotowe nabycie towarów (WNT)", "eksport i import towarów",
        "transakcje łańcuchowe i trójstronne", "miejsce świadczenia i import usług",
        "odwrotne obciążenie", "mechanizm podzielonej płatności (MPP)",
        "biała lista i należyta staranność", "nieodpłatne przekazania i świadczenia",
        "świadczenia kompleksowe", "faktury korygujące i korekty",
        "fakturowanie i KSeF", "kasy rejestrujące", "ulga na złe długi",
        "nieruchomości (dostawa, najem, zwolnienia)", "VAT e-commerce / OSS",
        "transakcje finansowe i ubezpieczeniowe (zwolnienia)",
        "prewspółczynnik JST i działalność mieszana",
        "klasyfikacja towarów/usług i stawka (WIS)", "inne zagadnienia VAT",
    ],
    "PIT": [
        "forma opodatkowania działalności (skala/liniowy/ryczałt)",
        "ryczałt od przychodów ewidencjonowanych",
        "przychody ze stosunku pracy i świadczenia pracownicze",
        "świadczenia nieodpłatne i ZFŚS", "PPK/PPE", "najem i dzierżawa",
        "sprzedaż nieruchomości i praw majątkowych",
        "koszty uzyskania przychodów w działalności",
        "amortyzacja i środki trwałe (PIT)",
        "kapitały pieniężne (odsetki, dywidendy, zbycie udziałów/akcji)",
        "waluty wirtualne (kryptoaktywa)", "ulgi i odliczenia",
        "ulgi zerowy PIT (młodzi, powrót, 4+, senior)", "IP Box i ulga B+R (PIT)",
        "obowiązki płatnika (zaliczki, PIT-11)",
        "rezydencja podatkowa i dochody zagraniczne",
        "podatek u źródła i należności licencyjne (PIT)",
        "działalność nierejestrowana", "przychody z innych źródeł",
        "darowizny i przychody nieodpłatne",
        "podróże służbowe, ryczałty i ekwiwalenty",
        "fundacja rodzinna (skutki dla beneficjentów w PIT)",
        "inne zagadnienia PIT",
    ],
    "AKCYZA": [
        "wyroby energetyczne (paliwa)", "energia elektryczna",
        "wyroby gazowe (gaz ziemny/LPG)",
        "napoje alkoholowe (alkohol etylowy, piwo, wino, wyroby pośrednie)",
        "wyroby tytoniowe", "susz tytoniowy",
        "płyn do e-papierosów i wyroby nowatorskie", "samochody osobowe",
        "zwolnienia ze względu na przeznaczenie",
        "zwolnienia dla energii elektrycznej (OZE, zakłady energochłonne)",
        "składy podatkowe i zawieszenie poboru akcyzy",
        "nabycie i dostawa wewnątrzwspólnotowa wyrobów akcyzowych",
        "import i eksport wyrobów akcyzowych", "ubytki wyrobów akcyzowych",
        "znaki akcyzy (banderole)",
        "podmiot pośredniczący/zużywający i rejestracja",
        "obowiązki ewidencyjne i deklaracyjne (w tym prosumenci)",
        "klasyfikacja wyrobów (kody CN)", "inne zagadnienia akcyzy",
    ],
    "PCC": [
        "umowa sprzedaży rzeczy i praw majątkowych",
        "umowa pożyczki",
        "umowa darowizny (przejęcie długów i ciężarów)",
        "umowa dożywocia i renta",
        "dział spadku i zniesienie współwłasności",
        "ustanowienie hipoteki",
        "umowa spółki i zmiana umowy spółki",
        "podwyższenie kapitału zakładowego i dopłaty",
        "przekształcenie, łączenie i podział spółek",
        # Najczęstszy realnie problem PCC — wyłączenie z art. 2 pkt 4 ustawy.
        # Świadomie osobna pozycja, a nie „inne”: gdyby wpadało do worka,
        # monitoring fraz przestałby to wyłapywać jako odrębne zagadnienie.
        "wyłączenie przy czynnościach opodatkowanych VAT",
        "zwolnienia podmiotowe i przedmiotowe (PCC)",
        "podstawa opodatkowania i wartość rynkowa",
        "obowiązek podatkowy, płatnik (notariusz), solidarność",
        "deklaracja PCC-3, terminy i zwrot podatku",
        "inne zagadnienia PCC",
    ],
}


# ---------------------------------------------------------------------------
# TAKSONOMIA Z BAZY
# ---------------------------------------------------------------------------
# Odczyt leniwy i buforowany. Buforujemy z ważnością, nie na zawsze: aplikacja
# Streamlit potrafi żyć godzinami, a zmiana taksonomii ma się w niej pojawić
# bez restartu.
#
# ZASADA NADRZĘDNA: awaria bazy NIE MOŻE zatrzymać streszczania. Każdy błąd
# kończy się cichym powrotem do stałych powyżej i jednym ostrzeżeniem na wyjściu.

_TAKSONOMIA_TTL_S = 900          # 15 minut
_taks_lock = threading.Lock()
_taks_bufor: dict = {"branze": None, "przedmioty": None, "wczytano": 0.0,
                     "zrodlo": "stałe w kodzie", "ostrzezono": False}


def _polaczenie_z_env():
    """Buduje połączenie z bazą na podstawie zmiennych środowiskowych.
    Zwraca None, gdy konfiguracji brak — wtedy po prostu zostają stałe."""
    try:
        import db_core
    except ImportError:
        return None

    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return db_core.SupabaseDB({"url": url})

    if all(os.environ.get(k) for k in ("SUPABASE_HOST", "SUPABASE_USER",
                                       "SUPABASE_PASSWORD")):
        return db_core.SupabaseDB({
            "host": os.environ["SUPABASE_HOST"],
            "port": os.environ.get("SUPABASE_PORT", "5432"),
            "database": os.environ.get("SUPABASE_DB", "postgres"),
            "user": os.environ["SUPABASE_USER"],
            "password": os.environ["SUPABASE_PASSWORD"],
            "sslmode": os.environ.get("SUPABASE_SSLMODE", "require"),
        })
    return None


def _pobierz_z_bazy(db) -> tuple[list[str], dict[str, list[str]]]:
    """Czyta AKTYWNE pozycje w zapisanej kolejności.

    Pozycje wycofane (aktywna = FALSE) świadomie pomijamy: nie chcemy, żeby
    model dalej je wybierał, ale muszą zostać w tabeli, bo powołują się na nie
    istniejące streszczenia i subskrypcje branż."""
    b = [r["branza"] for r in db.wykonaj(
        "SELECT branza FROM taksonomia_branze WHERE aktywna ORDER BY kolejnosc",
        fetch=True)]

    p: dict[str, list[str]] = {}
    for r in db.wykonaj(
            "SELECT podatek, przedmiot FROM taksonomia_przedmiotow "
            "WHERE aktywny ORDER BY podatek, kolejnosc", fetch=True):
        p.setdefault(r["podatek"], []).append(r["przedmiot"])

    return b, p


def zaladuj_taksonomie(db=None, *, wymus: bool = False) -> str:
    """
    Wczytuje taksonomię do bufora. Zwraca opis użytego źródła.

    db     — gotowe połączenie (Streamlit ma już swoje, po co otwierać drugie);
             gdy None, moduł spróbuje zbudować je ze zmiennych środowiskowych.
    wymus  — pomija bufor, przydatne zaraz po zmianie taksonomii.
    """
    with _taks_lock:
        swiezy = (time.time() - _taks_bufor["wczytano"]) < _TAKSONOMIA_TTL_S
        if not wymus and _taks_bufor["branze"] and swiezy:
            return _taks_bufor["zrodlo"]

        polaczenie = db or _polaczenie_z_env()
        if polaczenie is not None:
            try:
                b, p = _pobierz_z_bazy(polaczenie)
                # Pusta tabela to nie jest poprawna taksonomia — traktujemy ją
                # jak brak danych, żeby nie wysłać modelowi pustej listy.
                if b and p:
                    _taks_bufor.update({"branze": b, "przedmioty": p,
                                        "wczytano": time.time(),
                                        "zrodlo": "baza"})
                    return "baza"
            except Exception as e:
                if not _taks_bufor["ostrzezono"]:
                    print(f"[taksonomia] Nie udało się odczytać z bazy, "
                          f"używam stałych z kodu: {str(e).splitlines()[0][:120]}")
                    _taks_bufor["ostrzezono"] = True

        _taks_bufor.update({"branze": list(BRANZE),
                            "przedmioty": {k: list(v) for k, v in PRZEDMIOTY.items()},
                            "wczytano": time.time(),
                            "zrodlo": "stałe w kodzie"})
        return "stałe w kodzie"


def branze() -> list[str]:
    """Obowiązująca lista branż."""
    if not _taks_bufor["branze"]:
        zaladuj_taksonomie()
    return _taks_bufor["branze"]


def przedmioty(podatek: str) -> list[str]:
    """Obowiązująca lista przedmiotów dla podanego podatku (pusta, gdy brak)."""
    if not _taks_bufor["przedmioty"]:
        zaladuj_taksonomie()
    return _taks_bufor["przedmioty"].get((podatek or "").upper(), [])


def zrodlo_taksonomii() -> str:
    """Do diagnostyki: 'baza' albo 'stałe w kodzie'."""
    if not _taks_bufor["branze"]:
        zaladuj_taksonomie()
    return _taks_bufor["zrodlo"]


def _waliduj_przedmioty(surowe, podatek: str) -> list[str]:
    """Przycina do taksonomii przedmiotów WŁAŚCIWEJ dla podatku (bez wielkości
    liter); wartości spoza listy odrzuca. Zwraca JEDEN dominujący przedmiot
    (pierwszy trafiony z listy) — klasyfikacja jednowartościowa."""
    lista = przedmioty(podatek)
    if not surowe or not lista:
        return []
    if isinstance(surowe, str):
        surowe = re.split(r"[;]|,(?![^()]*\))", surowe)
    mapa = {p.lower(): p for p in lista}
    for s in surowe:
        p = mapa.get(str(s).strip().strip('"').lower())
        if p:
            return [p]
    return []


# Prompt systemowy MUSI powstawać leniwie: zawiera listę branż, a ta jest
# czytana z bazy dopiero przy pierwszym użyciu. Zbudowany w chwili importu
# — jak było wcześniej — zamroziłby stałe z kodu na całe życie procesu.
def _system_bazowy() -> str:
    return (
    "Jesteś asystentem polskiego doradcy podatkowego. Streszczasz polskie "
    "interpretacje indywidualne WIERNIE, wyłącznie na podstawie dostarczonej "
    "treści. Nie zmyślasz, nie dodajesz wiedzy spoza tekstu, nie oceniasz.\n\n"
    "BEZWZGLĘDNY WYMÓG JĘZYKA: całość — i temat, i streszczenie — MUSI być "
    "napisana PO POLSKU. Nawet jeśli fragmenty źródła są po angielsku, "
    "streszczasz PO POLSKU. Odpowiedź w innym języku niż polski jest błędem.\n\n"
    "Zwróć WYŁĄCZNIE obiekt JSON (bez komentarzy, bez bloków ```), o kluczach:\n"
    '  "temat"        — jedno zwięzłe zdanie po polsku: czego dotyczy '
    "interpretacja,\n"
    '  "streszczenie" — ciągła proza po polsku (NIE lista), maksymalnie 12 '
    "zdań, ujmująca: stan faktyczny lub zdarzenie przyszłe, pytanie "
    "wnioskodawcy, jego stanowisko oraz stanowisko organu z kluczowym "
    "uzasadnieniem,\n"
    '  "branze"       — lista 1–2 branż, których dotyczy działalność '
    "wnioskodawcy opisana w interpretacji. Oceniaj PO TREŚCI (czym zajmuje "
    "się wnioskodawca), nie po słowach kluczowych. Wybieraj WYŁĄCZNIE z tej "
    "listy (dokładna pisownia): " + "; ".join(branze()) + ". Jeżeli żadna nie "
    'pasuje wyraźnie, użyj "inna".'
    )



def _system_dla(podatek: str) -> str:
    """Bazowa instrukcja + (gdy znamy podatek) wymóg klasyfikacji przedmiotowej
    z listą właściwą dla tego podatku."""
    baza = _system_bazowy()
    lista = przedmioty(podatek)
    if not lista:
        return baza
    return baza + (
        '\nDodatkowo zwróć klucz "przedmiot" — DOKŁADNIE JEDEN, dominujący '
        "obszar merytoryczny, którego NAJBARDZIEJ dotyczy interpretacja "
        "(główny problem podatkowy). Wybierz WYŁĄCZNIE jedną pozycję z tej "
        "listy (dokładna pisownia): " + "; ".join(lista) + ". Jeżeli żadna "
        'pozycja nie pasuje wyraźnie, użyj ostatniej ("inne zagadnienia ...").'
    )


# ---------------------------------------------------------------------------
# PARSOWANIE ODPOWIEDZI — odporne na ucięty / niedomknięty JSON
# ---------------------------------------------------------------------------
def _odkoduj(val: str) -> str:
    for a, b in (('\\"', '"'), ("\\n", "\n"), ("\\t", "\t"), ("\\/", "/")):
        val = val.replace(a, b)
    return val.strip().rstrip('"').strip()


def _wytnij_pole(t: str, klucz: str) -> str:
    """Wyciąga wartość pola z (także niekompletnego) JSON-a.
    Najpierw poprawnie zamknięty string, a gdy odpowiedź jest ucięta —
    bierze wszystko od otwierającego cudzysłowu do końca."""
    m = re.search(r'"' + klucz + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', t, re.S)
    if not m:
        m = re.search(r'"' + klucz + r'"\s*:\s*"(.*)$', t, re.S)
    return _odkoduj(m.group(1)) if m else ""


def _waliduj_branze(surowe) -> list[str]:
    """Przycina do zamkniętej taksonomii (dopasowanie bez wielkości liter);
    wartości spoza listy odrzuca. Maksymalnie 2 branże."""
    if not surowe:
        return []
    if isinstance(surowe, str):
        surowe = re.split(r"[,;]", surowe)
    mapa = {b.lower(): b for b in branze()}
    wynik = []
    for s in surowe:
        b = mapa.get(str(s).strip().strip('"').lower())
        if b and b not in wynik:
            wynik.append(b)
    return wynik[:2]


def _wyodrebnij_json(tresc: str) -> dict:
    t = tresc.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()

    # Zdejmij wiodące linie-etykiety (np. „User Safety: safe”), które niektóre
    # darmowe modele doklejają przed właściwą treścią — inaczej wyciekłyby do
    # streszczenia, gdy odpowiedź nie jest w JSON.
    linie = t.splitlines()
    poczatek = 0
    while poczatek < len(linie) and _linia_smieciowa(linie[poczatek]):
        poczatek += 1
    t = "\n".join(linie[poczatek:]).strip()

    # 1) próba pełnego, poprawnego JSON-a
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            return {"temat": str(d.get("temat", "")).strip(),
                    "streszczenie": str(d.get("streszczenie", "")).strip(),
                    "branze": _waliduj_branze(d.get("branze")),
                    "przedmioty_raw": d.get("przedmiot", d.get("przedmioty"))}
        except Exception:
            pass

    # 2) ratunkowa ekstrakcja pól (obsługuje ucięty JSON — bez rusztowania)
    temat = _wytnij_pole(t, "temat")
    streszcz = _wytnij_pole(t, "streszczenie")
    m_br = re.search(r'"branze"\s*:\s*\[(.*?)\]', t, re.S)
    branze = _waliduj_branze(m_br.group(1)) if m_br else []
    m_pr = re.search(r'"przedmiot"\s*:\s*"([^"]*)"', t)
    if not m_pr:
        m_pr = re.search(r'"przedmiot[y]?"\s*:\s*\[(.*?)\]', t, re.S)
    przedm = m_pr.group(1) if m_pr else None
    if streszcz:
        return {"temat": temat, "streszczenie": streszcz, "branze": branze,
                "przedmioty_raw": przedm}

    # 3) ostatecznie: cała treść jako streszczenie, ale BEZ nagłówków JSON
    czysty = re.sub(r'^\s*\{?\s*"?(temat|streszczenie)"?\s*:\s*"?', "", t)
    return {"temat": temat, "streszczenie": _odkoduj(czysty), "branze": branze,
            "przedmioty_raw": None}


# ---------------------------------------------------------------------------
# WYKRYWANIE ODPOWIEDZI PO ANGIELSKU
# ---------------------------------------------------------------------------
_POLSKIE_ZNAKI = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
_ANG_SLOWA = ("the", "and", "of", "is", "are", "that", "this", "which", "for",
              "with", "applicant", "authority", "company", "tax", "costs",
              "income", "court", "provided", "concerning")


def _po_angielsku(t: str) -> bool:
    """Heurystyka: dłuższy tekst BEZ ani jednego polskiego znaku diakrytycznego,
    zawierający kilka angielskich słów funkcyjnych, to praktycznie na pewno
    odpowiedź po angielsku (12-zdaniowe polskie streszczenie prawne bez żadnego
    ą/ć/ę/ł/… nie występuje)."""
    if not t or len(t.strip()) < 120:
        return False
    if any(ch in _POLSKIE_ZNAKI for ch in t):
        return False
    low = " " + t.lower() + " "
    trafienia = sum(1 for w in _ANG_SLOWA if (" " + w + " ") in low)
    return trafienia >= 3


# Poprawne streszczenie interpretacji jest znacznie dłuższe niż jakikolwiek
# śmieciowy wtręt; poniżej tej granicy to prawie na pewno nie jest streszczenie.
_MIN_DLUGOSC = 120

# Wzorce „śmieci” zwracanych czasem przez darmowe modele zamiast treści:
# etykiety moderacji (np. „User Safety: safe”), odmowy, meta-adnotacje.
_WZORCE_SMIECI = re.compile(
    r"(user\s*safety|safety\s*[:=]\s*(safe|unsafe)|content\s*policy|"
    r"moderation|flagged|as an ai|i (cannot|can't|am unable|will not)|"
    r"nie mogę (streścić|pomóc|udzielić)|^\s*(safe|unsafe)\s*$)",
    re.I,
)


def _linia_smieciowa(ln: str) -> bool:
    return len(ln.strip()) < 40 and bool(_WZORCE_SMIECI.search(ln))


def streszczenie_wadliwe(s: str | None) -> bool:
    """Wspólna kontrola jakości (używana też przez moduł 6 i skrypt automatu).
    Odrzuca: puste, surowe/ucięte JSON-y, zbyt krótkie, etykiety bezpieczeństwa
    i odmowy oraz odpowiedzi po angielsku. Taki wpis nadaje się do ponownego
    wygenerowania."""
    if not s or not s.strip():
        return True
    t = s.strip()
    if t.startswith("{") or '"streszczenie"' in t or '"temat"' in t:
        return True
    if len(t) < _MIN_DLUGOSC:
        return True
    if _WZORCE_SMIECI.search(t):
        return True
    if _po_angielsku(t):
        return True
    return False


# ---------------------------------------------------------------------------
# GŁÓWNA FUNKCJA
# ---------------------------------------------------------------------------
def streszcz_tekst(
    tekst: str,
    sygnatura: str,
    data_wyd: str,
    *,
    api_key: str,
    model: str = MODEL_DOMYSLNY,
    podatek: str = "",
    limit_znakow: int = 20000,
    timeout: int = 90,
    proby: int = 3,
) -> dict:
    """Zwraca {"temat": ..., "streszczenie": ...} PO POLSKU.
    Rzuca RuntimeError po wyczerpaniu prób (limit 429, brak odpowiedzi
    albo uporczywa odpowiedź po angielsku)."""
    if not api_key:
        raise RuntimeError("Brak klucza API OpenRouter.")

    tresc_wejscia = (tekst or "")[:limit_znakow]
    user_bazowy = (f"Sygnatura: {sygnatura}\nData wydania: {data_wyd}\n\n"
                   f"TREŚĆ INTERPRETACJI:\n{tresc_wejscia}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://skaner-doradca.streamlit.app",
        "X-Title": "Skaner Doradca",
    }

    ostatni_blad = None
    dopisek_pl = ""  # wzmacniany, gdy model odpowie po angielsku
    for i in range(proby):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_dla(podatek)},
                {"role": "user", "content": user_bazowy + dopisek_pl},
            ],
            "temperature": 0.2,
            "max_tokens": MAKS_TOKENOW,
        }
        try:
            r = requests.post(ENDPOINT, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as e:
            ostatni_blad = f"Błąd sieci: {e}"
            time.sleep(3 * (i + 1))
            continue

        if r.status_code == 429:
            ostatni_blad = "Przekroczony limit zapytań (429)."
            time.sleep(6 * (i + 1))
            continue
        if r.status_code >= 400:
            try:
                info = r.json().get("error", {}).get("message", r.text[:200])
            except Exception:
                info = r.text[:200]
            ostatni_blad = f"HTTP {r.status_code}: {info}"
            if r.status_code in (401, 402, 403):
                raise RuntimeError(ostatni_blad)
            time.sleep(4 * (i + 1))
            continue

        try:
            dane = r.json()
            tresc = dane["choices"][0]["message"]["content"]
        except Exception as e:
            ostatni_blad = f"Nieoczekiwana odpowiedź: {e}"
            time.sleep(3 * (i + 1))
            continue

        if not tresc or not tresc.strip():
            ostatni_blad = "Pusta odpowiedź modelu."
            time.sleep(3 * (i + 1))
            continue

        wynik = _wyodrebnij_json(tresc)

        # Kontrola jakości — angielski, etykiety bezpieczeństwa, odmowy,
        # zbyt krótkie. Jeśli wadliwe: wzmocnij instrukcję i ponów.
        if streszczenie_wadliwe(wynik.get("streszczenie", "")):
            ostatni_blad = "Model zwrócił wadliwą/niepełną odpowiedź."
            if i < proby - 1:
                dopisek_pl = (
                    "\n\n[PRZYPOMNIENIE] Odpowiedz WYŁĄCZNIE po polsku, w formacie "
                    "JSON o kluczach \"temat\" i \"streszczenie\". Podaj samo "
                    "merytoryczne streszczenie interpretacji — bez etykiet "
                    "bezpieczeństwa, ocen, metadanych i komentarzy."
                )
                continue
            raise RuntimeError(
                "Model uporczywie zwraca wadliwą odpowiedź (np. etykietę "
                "bezpieczeństwa albo po angielsku) — zmień model i spróbuj ponownie."
            )

        wynik["przedmioty"] = _waliduj_przedmioty(
            wynik.pop("przedmioty_raw", None), podatek)
        return wynik

    raise RuntimeError(ostatni_blad or "Nie udało się uzyskać streszczenia.")
