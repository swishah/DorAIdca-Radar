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

OD 25.09.2026 (decyzja wlasciciela: „mniej, ale rowno", od najnowszych,
bez obchodzenia limitow MF):
  - miesiace OD NAJNOWSZYCH — swieze lata sa najbardziej przydatne w pracy;
  - KURSOR per podatek i miesiac (plan: kursory): przebieg przerwany limitem
    albo blokada wznawia sie od dnia, na ktorym stanal, a miesiac zakonczony
    ("KONIEC") nie wraca. Wczesniej kazda noc brala miesiac od 1. dnia:
    PIT 01.2023 noc w noc sciagal te same ~400 tresci i dostawal blokade
    (23.09: 362 pobrane, 25.09: 59 duplikatow i BLOKADA);
  - ZNANE: ID, ktore chmura juz ma w tym oknie, nie sa pobierane ponownie
    (do chmury idzie tylko zapytanie o ID, bez tresci);
  - 300 tresci na noc, 8–12 s przerwy (uzupelnianie_mf.py).

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
MAKS_PRZEBIEGU = 300          # tresci na jedno okno (miesiac)
MAKS_NOCY = 300               # tresci na jedna noc (suma okien)
KONIEC = "KONIEC"             # kursor miesiaca zakonczonego

# Jedno zadanie obchodzi kolejne okna, az wyczerpie budzet nocy. Dwa
# bezpieczniki, zeby nie wyjsc poza noc i poza limit zadania u GitHuba
# (6 godzin): najpozniejsza godzina UTC, o ktorej wolno ZACZAC nowe okno,
# i twardy limit dlugosci calego przebiegu.
KONIEC_OKNA_UTC = 3           # 03:00 UTC = 05:00 czasu polskiego latem
MAKS_DLUGOSC = timedelta(hours=4)
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


def nastepne_okno(db, pomin=(), kursory=None):
    """(podatek, miesiac, od, do, brakuje) — NAJNOWSZY miesiac z brakiem.

    Miesiac z kursorem KONIEC jest pomijany; z kursorem-data okno zaczyna sie
    od tej daty zamiast od 1. dnia miesiaca.

    Braki liczy widok braki_archiwum: EUREKA ma wiecej niz GREATEST(w_bazie,
    pobrane_nocami). Drugi skladnik jest konieczny, bo Docker potwierdza stan
    dopiero, gdy go wlaczysz — bez tego ten sam miesiac wracalby co noc.

    `pomin` to klucze „PODATEK|RRRR-MM" odwiedzone juz tej nocy. Bez tego
    miesiac, z ktorego nie da sie nic pobrac (EUREKA liczy dokument, ktorego
    nie ma jak sciagnac, albo data wydania wpada w inny miesiac niz w bazie),
    wracal w kazdym obrocie petli: pobrane=0 nie zmniejsza budzetu ani braku,
    wiec noc krecila sie w kolko na jednym zapytaniu do MF przez cztery godziny.
    """
    kursory = kursory or {}
    zamkniete = [k for k, v in kursory.items() if v == KONIEC]
    w = db.wykonaj("""SELECT podatek, miesiac, brakuje FROM braki_archiwum
                      WHERE NOT (podatek || '|' || miesiac = ANY(%s))
                      ORDER BY miesiac DESC, podatek LIMIT 1""",
                   (list(pomin) + zamkniete,), fetch=True) or []
    if not w:
        return None
    podatek, miesiac, brakuje = w[0]["podatek"], w[0]["miesiac"], w[0]["brakuje"]
    rok, mies = int(miesiac[:4]), int(miesiac[5:7])
    nastepny = date(rok + (mies == 12), 1 if mies == 12 else mies + 1, 1)
    od = kursory.get("%s|%s" % (podatek, miesiac)) or "%s-01" % miesiac
    return (podatek, miesiac, od, (nastepny - timedelta(days=1)).isoformat(), brakuje)


def zapisz_znane(db, podatek: str, od: str, do: str) -> str:
    """ID interpretacji z tego okna, ktore chmura juz ma — same ID, bez tresci."""
    w = db.wykonaj("""SELECT id FROM dokumenty
                      WHERE upper(podatek) = %s AND left(data_wyd, 10) BETWEEN %s AND %s""",
                   (podatek, od, do), fetch=True) or []
    plik = "znane.json"
    with open(plik, "w", encoding="utf-8") as f:
        json.dump([str(x["id"]) for x in w], f)
    return plik


def zanotuj_pobranie(db, podatek: str, miesiac: str, ile: int) -> None:
    """Ile pobralismy w tym miesiacu — zanim Docker potwierdzi wlasnym licznikiem."""
    db.wykonaj(
        """INSERT INTO kompletnosc_archiwum (podatek, miesiac, pobrane_nocami)
           VALUES (%s, %s, %s)
           ON CONFLICT (podatek, miesiac) DO UPDATE
           SET pobrane_nocami = kompletnosc_archiwum.pobrane_nocami + EXCLUDED.pobrane_nocami""",
        (podatek, miesiac, ile))


def odloz_czesc(numer: int) -> None:
    """Odklada wynik jednego okna pod wlasna nazwa.

    uzupelnianie_mf zapisuje zawsze wynik.json i dokumenty.json.gz, wiec bez
    tego kazde kolejne okno kasowaloby poprzednie i artefakt niosl by tylko
    ostatni miesiac. Numerowane czesci odbiera wykonawca w Dockerze
    (zadania/uzupelnianie.py, rozpakuj)."""
    katalog = uzupelnianie_mf.KATALOG
    for nazwa, nowa in (("wynik.json", "czesc-%02d-wynik.json" % numer),
                        ("dokumenty.json.gz", "czesc-%02d-dokumenty.json.gz" % numer)):
        stara = os.path.join(katalog, nazwa)
        if os.path.exists(stara):
            os.replace(stara, os.path.join(katalog, nowa))


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

    start = t
    # Reczne uruchomienie z podanym limitem robi JEDNO okno — sluzy do
    # sprawdzenia, nie do nadrabiania. Zapamietujemy przed petla, bo w srodku
    # sami wpisujemy MAKS_DOK dla kazdego okna.
    jedno_okno = bool(os.environ.get("MAKS_DOK"))
    reczny_limit = int(os.environ.get("MAKS_DOK") or 0)
    # Do ktorej godziny wolno ZACZYNAC nowe okno. Liczone od startu, wiec
    # przebieg reczny w dzien nie konczy sie po pierwszym miesiacu.
    koniec_nocy = start.replace(hour=KONIEC_OKNA_UTC, minute=0, second=0, microsecond=0)
    if koniec_nocy <= start:
        koniec_nocy += timedelta(days=1)
    kursory = dict(stan.get("kursory") or {})
    okien, pobrane_lacznie = 0, 0
    odwiedzone = []          # kazdy miesiac najwyzej raz na noc — patrz nastepne_okno
    while budzet > 0:
        teraz = datetime.now(timezone.utc)
        # Nowego okna nie zaczynamy po godzinie zamkniecia nocy ani po czterech
        # godzinach pracy — biezace okno zawsze konczymy w calosci, bo urwane
        # pobieranie to zmarnowany ruch u MF.
        if okien and teraz >= koniec_nocy:
            print("Koniec okna nocnego (%02d:00 UTC) — nie zaczynam kolejnego miesiaca."
                  % KONIEC_OKNA_UTC)
            break
        if teraz - start > MAKS_DLUGOSC:
            print("Przebieg trwa juz ponad %s — konczę." % MAKS_DLUGOSC)
            break

        okno = nastepne_okno(db, odwiedzone, kursory)
        if not okno:
            if odwiedzone:
                print("Kazdy miesiac z brakiem byl juz tej nocy odwiedzony — koniec.")
            else:
                zapisz(db, {"ukonczono": teraz.isoformat()})
                print("Nie ma czego uzupelniac — archiwum kompletne wedlug audytu.")
            break

        kod, miesiac, od, do, brakuje = okno
        odwiedzone.append("%s|%s" % (kod, miesiac))
        if not przepisy.get(kod):
            print("Brak numeru przepisu dla podatku %s — pomijam." % kod)
            break

        maks = min(reczny_limit or MAKS_PRZEBIEGU, MAKS_PRZEBIEGU, budzet)
        print("\n[okno %d] %s %s (brakuje %d), najwyzej %d tresci (budzet nocy: %d)."
              % (okien + 1, kod, miesiac, brakuje, maks, budzet))
        if sucho:
            print("SUCHO=1 — koncze bez pytania MF.")
            return 0

        # uzupelnianie_mf czyta wejscie ze srodowiska — ta sama droga co przy
        # zleceniu z Dockera, wiec pobieranie zachowuje sie identycznie.
        os.environ["PODATEK"] = kod
        os.environ["PRZEPIS"] = str(przepisy[kod])
        os.environ["OD"], os.environ["DO"] = od, do
        os.environ["MAKS_DOK"] = str(maks)
        os.environ["ZNANE"] = zapisz_znane(db, kod, od, do)
        try:
            uzupelnianie_mf.main()
        except SystemExit as e:                  # zle wejscie — plan jest chory
            zapisz(db, {"blad": "uzupelnianie_mf: %s" % str(e)[:300],
                        "wstrzymane_do": (teraz + WSTRZYMANIE).isoformat(),
                        "powod": "blad wejscia %s %s..%s" % (kod, od, do)})
            raise

        with open(os.path.join(uzupelnianie_mf.KATALOG, "wynik.json"), encoding="utf-8") as f:
            wynik = json.load(f)
        odloz_czesc(okien + 1)

        status = wynik.get("status") or "?"
        pobrane = wynik.get("pobrane", 0)
        okien += 1
        pobrane_lacznie += pobrane
        budzet -= pobrane

        # Audyt jest pamiecia postepu: dopisujemy, ile z tego miesiaca
        # pobralismy. Gdy Docker w koncu policzy swoje, jego liczba rozstrzyga.
        zanotuj_pobranie(db, kod, miesiac, pobrane)
        pod = podatki.get(kod)
        if pod is not None:
            pod["pobrane"] = pod.get("pobrane", 0) + pobrane
            pod["status"], pod["ostatnio"] = status, teraz.isoformat()

        # Kursor: zakonczony miesiac nie wraca, przerwany wznawia sie od dnia,
        # na ktorym stanal — nie od 1. dnia miesiaca.
        klucz_okna = "%s|%s" % (kod, miesiac)
        if status == "OK":
            kursory[klucz_okna] = KONIEC
        elif wynik.get("nastepne_od"):
            kursory[klucz_okna] = wynik["nastepne_od"]

        stan = wczytaj(db)
        zmiany = {
            "kursory": kursory,
            "podatki": podatki,
            "noc": {"data": teraz.astimezone(PL).date().isoformat(),
                    "pobrane": pobrane_tej_nocy(stan, teraz) + pobrane},
            "historia": ([{"kiedy": teraz.isoformat(), "podatek": kod, "miesiac": miesiac,
                           "od": od, "do": do,
                           "status": status, "lista": wynik.get("lista"), "pobrane": pobrane,
                           "znane": wynik.get("znane", 0),
                           "przebieg": os.environ.get("GITHUB_RUN_ID", "")}]
                         + (stan.get("historia") or []))[:HISTORIA],
            "blad": "",
        }
        # Oznaka blokady albo niepelna lista = doba przerwy i koniec nocy.
        # MF wazniejsze od tempa: blokada adresow GitHuba zatrzymalaby takze
        # codzienna synchronizacje.
        if status in ("BLOKADA", "NIEPELNA_LISTA"):
            zmiany["wstrzymane_do"] = (teraz + WSTRZYMANIE).isoformat()
            zmiany["powod"] = "%s — %s %s" % (status, kod, miesiac)
            zapisz(db, zmiany)
            print("Status %s — wstrzymuje uzupelnianie na dobe i koncze noc." % status)
            break
        zapisz(db, zmiany)
        print("[okno %d] %s %s: status %s, pobrane %d." % (okien, kod, miesiac, status, pobrane))

        if jedno_okno:
            break

    print("\nNoc: %d okien, pobrane %d tresci (budzet %d)."
          % (okien, pobrane_lacznie, MAKS_NOCY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
