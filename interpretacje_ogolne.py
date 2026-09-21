#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
interpretacje_ogolne.py — interpretacje ogolne z EUREKI do chmury.

PO CO OSOBNY SKRYPT
  Interpretacja ogolna wiaze organy w sprawach tego samego rodzaju, wiec jedna
  potrafi przesadzic sprawe, ktorej nie rozstrzyga sto indywidualnych. Jest ich
  przy tym garstka: od 1.01.2024 do 21.09.2026 — DZIEWIETNASCIE. Pojawiaja sie
  kilka razy w roku.

  Ta dysproporcja (kilkanascie kontra kilkadziesiat tysiecy) jest powodem, dla
  ktorego nie doklejamy ich do synchronizacji dziennej: tamta chodzi w oknie
  ruchomym, dzieli okresy przy tysiacu wynikow i pilnuje kompletnosci po
  podatkach. Tutaj wystarczy jedno zapytanie o cala liste od 2024 roku
  i pobranie tresci tych, ktorych jeszcze nie mamy.

SKAD
  EUREKA, kategoria informacji nr 3 („Interpretacja ogolna" w slowniku
  KATEGORIA_INFORMACJI; nr 1 to indywidualna). Te same adresy, te same
  naglowki i ta sama ostroznosc co przy indywidualnych — tresci po jednej,
  z przerwa, nigdy rownolegle.

DOKAD
  Tabela interpretacje_ogolne w CHMURZE (schemat 23). Tresci ida tu do chmury,
  inaczej niz zaleglosci indywidualne: to ~0,5 MB, a panel czyta baze
  w Dockerze i musi je dostac synchronizacja.

URUCHOMIENIE
  python interpretacje_ogolne.py             # lista + tresci brakujacych
  python interpretacje_ogolne.py --sucho     # tylko pokaz, co by zrobil
  python interpretacje_ogolne.py --od 2020-01-01
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.request
from datetime import date

import db_core
import utils

KATEGORIA_OGOLNA = 3
DATA_START = os.environ.get("OGOLNE_OD", "").strip() or "2024-01-01"
API = "https://eureka.mf.gov.pl/api/public/v1/wyszukiwarka/informacje/"
# Ta sama przerwa co przy tresciach indywidualnych. Dokumentow jest
# kilkanascie, wiec caly przebieg i tak trwa minute.
PRZERWA_MIN_S, PRZERWA_MAX_S = 4.0, 7.0


def _naglowki() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Referer": "https://eureka.mf.gov.pl/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def lista(od: str, do: str) -> list:
    """Metadane wszystkich interpretacji ogolnych z okresu. Jedno zapytanie —
    kilkanascie pozycji miesci sie na jednej stronie wynikow."""
    payload = {
        "query": "",
        "filter": {"KATEGORIA_INFORMACJI": [KATEGORIA_OGOLNA],
                   "DT_WYD_start": od, "DT_WYD_end": do},
        "columns": ["SYG", "ID_INFORMACJI", "DT_WYD", "KATEGORIA_INFORMACJI"],
        "searchInFullPhrase": False, "searchInContent": True,
        "searchInSynonyms": True, "warunkiDodatkowe": [],
    }
    url = API + "?size=200&page=0&sort=parametryPozycjonowania%2Casc"
    zadanie = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=_naglowki())
    with urllib.request.urlopen(zadanie, timeout=60) as r:
        dane = json.load(r)
    pozycje = []
    for w in dane.get("results") or []:
        idi = str(w.get("ID_INFORMACJI") or "").strip()
        syg = str(w.get("SYG") or "").strip()
        if idi and syg:
            pozycje.append({"id": idi, "sygnatura": syg,
                            "data_wyd": str(w.get("DT_WYD") or "")[:10],
                            "link": utils.PODGLAD_URL.format(id=idi)})
    return pozycje


def brakujace(db, pozycje: list) -> list:
    """Te, ktorych nie ma w bazie albo stoja bez tresci."""
    if not pozycje:
        return []
    maja = db.wykonaj(
        "SELECT id FROM interpretacje_ogolne WHERE id = ANY(%s) AND length(tekst) > 500",
        ([p["id"] for p in pozycje],), fetch=True) or []
    znane = {w["id"] for w in maja}
    return [p for p in pozycje if p["id"] not in znane]


def zapisz(db, pozycja: dict, tekst: str) -> None:
    db.wykonaj(
        """INSERT INTO interpretacje_ogolne (id, sygnatura, data_wyd, link, tekst)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET
             sygnatura = EXCLUDED.sygnatura,
             data_wyd  = EXCLUDED.data_wyd,
             link      = EXCLUDED.link,
             -- Tresc nadpisujemy tylko, gdy nowa jest pelniejsza: nieudane
             -- pobranie nie moze skasowac tego, co juz mamy.
             tekst     = CASE WHEN length(EXCLUDED.tekst) > length(interpretacje_ogolne.tekst)
                              THEN EXCLUDED.tekst ELSE interpretacje_ogolne.tekst END""",
        (pozycja["id"], pozycja["sygnatura"], pozycja["data_wyd"], pozycja["link"], tekst))


def main() -> int:
    sucho = "--sucho" in sys.argv
    od = DATA_START
    if "--od" in sys.argv:
        od = sys.argv[sys.argv.index("--od") + 1]
    do = date.today().isoformat()

    db = db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
    })

    pozycje = lista(od, do)
    print("EUREKA: %d interpretacji ogolnych w okresie %s..%s" % (len(pozycje), od, do))
    do_pobrania = brakujace(db, pozycje)
    print("Bez tresci w bazie: %d" % len(do_pobrania))
    for p in do_pobrania:
        print("  %s  %s" % (p["data_wyd"], p["sygnatura"]))
    if sucho:
        print("\n--sucho: nic nie pobieram i nie zapisuje.")
        return 0

    pobrane, puste = 0, 0
    with utils.requests.Session() as sesja:
        for p in do_pobrania:
            time.sleep(random.uniform(PRZERWA_MIN_S, PRZERWA_MAX_S))
            tekst, status = utils.pobierz_tekst_pdf(p["id"], sesja=sesja)
            if status == "BLOKADA":
                # Ta sama zasada co wszedzie: pierwsza oznaka blokady konczy
                # przebieg bez ponawiania. Reszta poczeka do jutra — przy
                # kilkunastu dokumentach rocznie nic sie nie stanie.
                print("Oznaka blokady przy %s — koncze przebieg." % p["sygnatura"])
                break
            if not tekst:
                puste += 1
                print("  %s: brak pobieralnej tresci" % p["sygnatura"])
                continue
            zapisz(db, p, tekst)
            pobrane += 1
            print("  %s: %d znakow" % (p["sygnatura"], len(tekst)))

    czeka = (db.wykonaj("SELECT count(*) AS ile FROM interpretacje_ogolne "
                        "WHERE length(tekst) > 500 AND coalesce(streszczenie, '') = ''",
                        fetch=True) or [{}])[0].get("ile", 0)
    print("\nPobrano %d, bez tresci %d. Czeka na streszczenie: %d." % (pobrane, puste, czeka))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
