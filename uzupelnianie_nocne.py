#!/usr/bin/env python3
"""
uzupelnianie_nocne.py — nocne uzupelnianie archiwum z MF, bez udzialu laptopa.

SKAD BIERZE SIE OKNO (od 21.09.2026)
  Z tabeli kompletnosc_archiwum: najstarszy miesiac, w ktorym EUREKA ma wiecej
  dokumentow niz my. Wczesniej zakres wyliczala regula „od 2023 do dnia przed
  najstarsza interpretacja w bazie" — nie widziala dziur w srodku (VAT: luty
  2024 – grudzien 2025) i uznawala niepelny miesiac za zrobiony (styczen 2024
  dla VAT-u: 303 w bazie, 781 w EURECE). Liczby do tabeli wpisuja audyt
  (audyt_archiwum.py) i wykonawca w Dockerze.

CO SIE ZMIENILO
  Wczesniej okno dat wybieral wykonawca w Dockerze i to on zlecal przebieg.
  Skutek: uzupelnianie chodzilo tylko wtedy, gdy laptop byl wlaczony w nocy,
  czyli — jak pokazal wrzesien 2026 — nigdy. Teraz plan trzyma chmura
  (tabela uzupelnianie_stan), okno wybiera ten skrypt, a Docker ma juz tylko
  jedno zadanie: odebrac artefakt, gdy go wlaczysz.

  TRESCI INTERPRETACJI NADAL OMIJAJA CHMURE. Do Supabase idzie wylacznie plan
  (kilka kilobajtow); dokumenty jada artefaktem GitHuba prosto do Dockera.

OSTROZNOSC WOBEC MF — bez zmian, tylko pilnuje jej ten skrypt zamiast Dockera:
  - tylko w oknie nocnym (harmonogram workflowu), z dala od synchronizacji;
  - najwyzej MAKS_PRZEBIEGU tresci na przebieg i MAKS_NOCY na noc;
  - pierwsza oznaka blokady wstrzymuje uzupelnianie na WSTRZYMANIE godzin —
    wznawia tylko czlowiek albo uplyw czasu.

WEJSCIE (zmienne srodowiskowe)
  SUPABASE_*   polaczenie z chmura (plan, nie tresci)
  MAKS_DOK     nadpisuje limit przebiegu — do uruchomien recznych
  SUCHO        '1' = pokaz, co by zrobil, i nie ruszaj MF

WYJSCIE
  wynik/wynik.json, wynik/dokumenty.json.gz — jak dotad, artefakt dla Dockera.
  Gdy nie ma czego robic, katalog wynik/ zostaje pusty i workflow konczy sie
  bez artefaktu.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import db_core
import utils
import uzupelnianie_mf


def utils_kody() -> dict:
    """Numery aktow prawnych w slowniku EUREKI dla podatkow wbudowanych."""
    return dict(utils.KODY_PRZEPISOW)

KLUCZ = "uzupelnianie_mf"
MAKS_PRZEBIEGU = 400          # tresci na jeden przebieg
MAKS_NOCY = 1500              # tresci na jedna noc (suma przebiegow)
WSTRZYMANIE = timedelta(hours=24)
HISTORIA = 15

# Doba liczona po polsku: przebiegi 22:45–02:45 UTC naleza do TEJ SAMEJ nocy,
# choc po UTC wypadaja na dwie daty. Bez tego budzet nocy zerowalby sie
# w srodku okna i limit 1500 nie mialby znaczenia.
PL = timezone(timedelta(hours=2))


def _polacz():
    return db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
    })


def wczytaj(db) -> dict:
    w = db.wykonaj("SELECT dane FROM uzupelnianie_stan WHERE klucz = %s",
                   (KLUCZ,), fetch=True)
    return (w[0]["dane"] if w else None) or {}


def zapisz(db, zmiany: dict) -> None:
    """Scala zmiany ze stanem (||), zeby rownolegly zapis nie wymazal reszty."""
    db.wykonaj(
        """INSERT INTO uzupelnianie_stan (klucz, dane) VALUES (%s, %s::jsonb)
           ON CONFLICT (klucz) DO UPDATE
           SET dane = uzupelnianie_stan.dane || EXCLUDED.dane, zmieniono = now()""",
        (KLUCZ, json.dumps(zmiany, ensure_ascii=False)))


def nastepne_okno(db):
    """(podatek, miesiac, od, do, brakuje) — najstarszy miesiac z brakiem.

    Braki liczy widok braki_archiwum: EUREKA ma wiecej niz GREATEST(w_bazie,
    pobrane_nocami). Drugi skladnik jest konieczny, bo Docker potwierdza stan
    dopiero, gdy go wlaczysz — bez tego ten sam miesiac wracalby co noc.
    """
    w = db.wykonaj("""SELECT podatek, miesiac, brakuje FROM braki_archiwum
                      ORDER BY miesiac, podatek LIMIT 1""", fetch=True) or []
    if not w:
        return None
    podatek, miesiac, brakuje = w[0]["podatek"], w[0]["miesiac"], w[0]["brakuje"]
    rok, mies = int(miesiac[:4]), int(miesiac[5:7])
    nastepny = date(rok + (mies == 12), 1 if mies == 12 else mies + 1, 1)
    return (podatek, miesiac, "%s-01" % miesiac,
            (nastepny - timedelta(days=1)).isoformat(), brakuje)


def zanotuj_pobranie(db, podatek: str, miesiac: str, ile: int) -> None:
    """Ile pobralismy w tym miesiacu — zanim Docker potwierdzi wlasnym licznikiem."""
    db.wykonaj(
        """INSERT INTO kompletnosc_archiwum (podatek, miesiac, pobrane_nocami)
           VALUES (%s, %s, %s)
           ON CONFLICT (podatek, miesiac) DO UPDATE
           SET pobrane_nocami = kompletnosc_archiwum.pobrane_nocami + EXCLUDED.pobrane_nocami""",
        (podatek, miesiac, ile))


def pobrane_tej_nocy(stan: dict, t: datetime) -> int:
    noc = stan.get("noc") or {}
    return noc.get("pobrane", 0) if noc.get("data") == t.astimezone(PL).date().isoformat() else 0


def main() -> int:
    t = datetime.now(timezone.utc)
    sucho = os.environ.get("SUCHO", "").strip() == "1"
    db = _polacz()
    stan = wczytaj(db)

    # Plan (stan.podatki) sluzy juz tylko do numerow przepisow w EURECE —
    # o tym, CO pobrac, decyduje audyt kompletnosci.
    podatki = stan.get("podatki") or {}
    przepisy = {kod: (p.get("przepis") or utils_kody().get(kod))
                for kod, p in podatki.items()}
    for kod, nr in utils_kody().items():
        przepisy.setdefault(kod, nr)

    # Wylacznik z Harmonogramu. Panel pisze go do bazy w Dockerze, a wykonawca
    # przenosi do chmury — tutaj widac juz tylko wynik. Brak klucza = wlaczone,
    # zeby swiezo zasiany plan ruszyl bez dodatkowego klikania.
    if not stan.get("wlaczone", True):
        print("Uzupelnianie wylaczone w Harmonogramie — nie ruszam MF.")
        return 0

    wstrzymane = stan.get("wstrzymane_do")
    if wstrzymane and t < datetime.fromisoformat(wstrzymane):
        print("Wstrzymane do %s (%s) — nie ruszam MF."
              % (wstrzymane, stan.get("powod") or "bez powodu"))
        return 0

    budzet = MAKS_NOCY - pobrane_tej_nocy(stan, t)
    if budzet <= 0:
        print("Budzet nocy wyczerpany (%d tresci). Koniec na dzis." % MAKS_NOCY)
        return 0

    okno = nastepne_okno(db)
    if not okno:
        zapisz(db, {"ukonczono": t.isoformat()})
        print("Nie ma czego uzupelniac — archiwum kompletne wedlug audytu.")
        return 0

    kod, miesiac, od, do, brakuje = okno
    if not przepisy.get(kod):
        print("Brak numeru przepisu dla podatku %s — pomijam." % kod)
        return 0
    maks = min(int(os.environ.get("MAKS_DOK") or MAKS_PRZEBIEGU), MAKS_PRZEBIEGU, budzet)
    print("Okno: %s %s (brakuje %d), najwyzej %d tresci (budzet nocy: %d)."
          % (kod, miesiac, brakuje, maks, budzet))
    if sucho:
        print("SUCHO=1 — koncze bez pytania MF.")
        return 0

    # uzupelnianie_mf czyta wejscie ze srodowiska — ta sama droga co przy
    # zleceniu z Dockera, wiec pobieranie zachowuje sie identycznie.
    os.environ["PODATEK"] = kod
    os.environ["PRZEPIS"] = str(przepisy[kod])
    os.environ["OD"], os.environ["DO"] = od, do
    os.environ["MAKS_DOK"] = str(maks)
    try:
        uzupelnianie_mf.main()
    except SystemExit as e:                      # zle wejscie — plan jest chory
        zapisz(db, {"blad": "uzupelnianie_mf: %s" % str(e)[:300],
                    "wstrzymane_do": (t + WSTRZYMANIE).isoformat(),
                    "powod": "blad wejscia %s %s..%s" % (kod, od, do)})
        raise

    with open(os.path.join(uzupelnianie_mf.KATALOG, "wynik.json"), encoding="utf-8") as f:
        wynik = json.load(f)

    status = wynik.get("status") or "?"
    pobrane = wynik.get("pobrane", 0)
    # Audyt jest teraz pamiecia postepu: dopisujemy, ile z tego miesiaca
    # pobralismy. Gdy Docker w koncu policzy swoje, jego liczba rozstrzyga.
    zanotuj_pobranie(db, kod, miesiac, pobrane)
    pod = podatki.get(kod)
    if pod is not None:
        pod["pobrane"] = pod.get("pobrane", 0) + pobrane
        pod["status"], pod["ostatnio"] = status, t.isoformat()

    zmiany = {
        "podatki": podatki,
        "noc": {"data": t.astimezone(PL).date().isoformat(),
                "pobrane": pobrane_tej_nocy(stan, t) + pobrane},
        "historia": ([{"kiedy": t.isoformat(), "podatek": kod, "miesiac": miesiac,
                       "od": od, "do": do,
                       "status": status, "lista": wynik.get("lista"), "pobrane": pobrane,
                       "przebieg": os.environ.get("GITHUB_RUN_ID", "")}]
                     + (stan.get("historia") or []))[:HISTORIA],
        "blad": "",
    }
    # Oznaka blokady albo niepelna lista = doba przerwy. MF wazniejsze od tempa:
    # blokada adresow GitHuba zatrzymalaby takze codzienna synchronizacje.
    if status in ("BLOKADA", "NIEPELNA_LISTA"):
        zmiany["wstrzymane_do"] = (t + WSTRZYMANIE).isoformat()
        zmiany["powod"] = "%s — %s %s..%s" % (status, kod, od, do)
        print("Status %s — wstrzymuje uzupelnianie na dobe." % status)
    zapisz(db, zmiany)
    print("Zapisano stan: %s %s, status %s, pobrane %d." % (kod, miesiac, status, pobrane))
    return 0


if __name__ == "__main__":
    sys.exit(main())
