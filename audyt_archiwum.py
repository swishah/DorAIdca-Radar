#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audyt_archiwum.py — ile dokumentow ma EUREKA w kazdym miesiacu.

PO CO
  Uzupelnianie zaleglosci wybieralo zakres regula „od 2023 do dnia przed
  najstarsza interpretacja w bazie". Regula gubila dokumenty na dwa sposoby:
  nie widziala dziur w srodku (VAT: luty 2024 – grudzien 2025, ~13 tys. pozycji)
  i uznawala niepelny miesiac za zrobiony (styczen 2024 dla VAT-u: 303 w bazie,
  781 w EURECE).

  Ten skrypt mierzy to, czego tamta regula sie domyslala. Dla kazdej pary
  (podatek, miesiac) pyta EUREKE o SAMA LICZBE wynikow i zapisuje ja w tabeli
  kompletnosc_archiwum (schemat 24).

DLACZEGO TO TANIE
  Zapytanie z size=1 oddaje pole totalHits i zadnej tresci — 0,4 sekundy.
  Pelny audyt szesciu podatkow od 2023 roku to ~270 zapytan, czyli osiem minut
  ruchu z pauza. Potem wystarcza kilkanascie zapytan na noc, bo stare miesiace
  zmieniaja sie rzadko (choc zmieniaja: MF dopublikowuje wstecz — dlatego
  odswiezamy najdawniej sprawdzane, a nie sprawdzamy raz na zawsze).

OSTROZNOSC WOBEC MF
  Ta sama pauza co wszedzie i twardy limit zapytan na przebieg. Pierwsza oznaka
  blokady konczy przebieg — audyt moze poczekac do jutra, blokada adresow
  GitHuba zatrzymalaby cala synchronizacje.

WEJSCIE (zmienne srodowiskowe)
  SUPABASE_*     polaczenie z chmura
  AUDYT_MAKS     ile par (podatek, miesiac) w jednym przebiegu (domyslnie 60)
  AUDYT_OD       poczatek zakresu, RRRR-MM (domyslnie 2023-01)
  AUDYT_WSZYSTKO '1' = sprawdz takze te sprawdzone niedawno (pelny audyt)

URUCHOMIENIE
  python audyt_archiwum.py
  python audyt_archiwum.py --sucho
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date

import db_core
import utils

OD_DOMYSLNIE = os.environ.get("AUDYT_OD", "").strip() or "2023-01"
MAKS_W_PRZEBIEGU = int(os.environ.get("AUDYT_MAKS") or 60)
# Miesiac sprawdzony w ciagu ostatnich DNI_SWIEZOSCI dni nie wraca do kolejki,
# chyba ze AUDYT_WSZYSTKO=1. Biezacy i poprzedni miesiac odswiezamy czesciej,
# bo tam liczby faktycznie rosna.
DNI_SWIEZOSCI = 30
DNI_SWIEZOSCI_BIEZACE = 1
PAUZA_S = float(os.environ.get("MF_PAUZA_STRONY_S", "1.5"))


def _miesiace(od: str) -> list:
    """Lista 'RRRR-MM' od `od` do biezacego miesiaca wlacznie."""
    rok, mies = int(od[:4]), int(od[5:7])
    dzis = date.today()
    wynik = []
    while (rok, mies) <= (dzis.year, dzis.month):
        wynik.append("%04d-%02d" % (rok, mies))
        mies += 1
        if mies > 12:
            rok, mies = rok + 1, 1
    return wynik


def _granice(miesiac: str) -> tuple:
    rok, mies = int(miesiac[:4]), int(miesiac[5:7])
    nast = date(rok + (mies == 12), 1 if mies == 12 else mies + 1, 1)
    return "%s-01" % miesiac, (nast - __import__("datetime").timedelta(days=1)).isoformat()


def podatki(db) -> dict:
    """{'VAT': 29955, ...} — wbudowane plus dodane z EUREKI."""
    mapa = dict(utils.KODY_PRZEPISOW)
    for w in db.wykonaj("SELECT kod, przepis_id FROM podatki_eureka WHERE aktywny",
                        fetch=True) or []:
        try:
            mapa[str(w["kod"]).upper()] = int(w["przepis_id"])
        except (TypeError, ValueError):
            continue
    return mapa


def do_sprawdzenia(db, mapa: dict, wszystko: bool) -> list:
    """Pary (podatek, miesiac) najdawniej sprawdzane — najpierw te, o ktore
    nigdy nie pytalismy."""
    biezacy = date.today().strftime("%Y-%m")
    poprzedni = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    wszystkie = [(p, m) for p in sorted(mapa) for m in _miesiace(OD_DOMYSLNIE)]

    znane = {}
    for w in db.wykonaj(
            "SELECT podatek, miesiac, sprawdzono_eureka FROM kompletnosc_archiwum",
            fetch=True) or []:
        znane[(w["podatek"], w["miesiac"])] = w["sprawdzono_eureka"]

    import datetime as dt
    teraz = dt.datetime.now(dt.timezone.utc)
    kolejka = []
    for para in wszystkie:
        kiedy = znane.get(para)
        if kiedy is None:
            kolejka.append((dt.datetime.min.replace(tzinfo=dt.timezone.utc), para))
            continue
        if wszystko:
            kolejka.append((kiedy, para))
            continue
        prog = DNI_SWIEZOSCI_BIEZACE if para[1] in (biezacy, poprzedni) else DNI_SWIEZOSCI
        if (teraz - kiedy).days >= prog:
            kolejka.append((kiedy, para))
    kolejka.sort()
    return [para for _, para in kolejka[:MAKS_W_PRZEBIEGU]]


def ile_w_eurece(sesja, kod_przepisu: int, od: str, do: str) -> int:
    """Sama liczba wynikow — size=1, czytamy totalHits, zadnej tresci."""
    url = utils.SEARCH_API_URL_BASE.format(size=1, page=0)
    payload = {
        "query": "",
        "filter": {"KATEGORIA_INFORMACJI": [1], "PRZEPISY": [kod_przepisu],
                   "DT_WYD_start": od, "DT_WYD_end": do},
        "columns": ["ID_INFORMACJI"],
        "searchInFullPhrase": False, "searchInContent": True,
        "searchInSynonyms": True, "warunkiDodatkowe": [],
    }
    _, status, total = utils._wykonaj_zapytanie_api(sesja, url, payload, timeout=30)
    if status == "ERROR":
        raise RuntimeError("BLOKADA")
    return int(total or 0)


def zapisz(db, podatek: str, miesiac: str, ile: int) -> None:
    db.wykonaj(
        """INSERT INTO kompletnosc_archiwum (podatek, miesiac, w_eurece, sprawdzono_eureka)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (podatek, miesiac) DO UPDATE
           SET w_eurece = EXCLUDED.w_eurece, sprawdzono_eureka = now()""",
        (podatek, miesiac, ile))


def main() -> int:
    sucho = "--sucho" in sys.argv
    wszystko = os.environ.get("AUDYT_WSZYSTKO", "").strip() == "1"
    db = db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
    })

    mapa = podatki(db)
    kolejka = do_sprawdzenia(db, mapa, wszystko)
    print("Podatki: %s" % ", ".join(sorted(mapa)))
    print("Do sprawdzenia w tym przebiegu: %d par (podatek, miesiac)." % len(kolejka))
    if sucho:
        for p, m in kolejka[:20]:
            print("  %-8s %s" % (p, m))
        print("--sucho: nie pytam MF.")
        return 0
    if not kolejka:
        print("Wszystko sprawdzone niedawno — nic do roboty.")
        return 0

    sprawdzone, blokada = 0, False
    with utils.requests.Session() as sesja:
        for podatek, miesiac in kolejka:
            od, do = _granice(miesiac)
            try:
                ile = ile_w_eurece(sesja, mapa[podatek], od, do)
            except RuntimeError:
                # Pierwsza oznaka blokady konczy przebieg. Audyt moze poczekac.
                print("Oznaka blokady przy %s %s — koncze przebieg." % (podatek, miesiac))
                blokada = True
                break
            zapisz(db, podatek, miesiac, ile)
            sprawdzone += 1
            time.sleep(PAUZA_S)

    braki = db.wykonaj(
        """SELECT count(*) AS miesiecy, coalesce(sum(brakuje), 0) AS dokumentow
           FROM braki_archiwum""", fetch=True) or [{}]
    b = braki[0] if braki else {}
    print("Sprawdzono %d par%s." % (sprawdzone, " (przerwane)" if blokada else ""))
    print("Braki w archiwum: %s dokumentow w %s miesiacach."
          % (b.get("dokumentow", "?"), b.get("miesiecy", "?")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
