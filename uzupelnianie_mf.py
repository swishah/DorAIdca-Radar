#!/usr/bin/env python3
"""
uzupelnianie_mf.py — zaległe interpretacje indywidualne z MF, bez zapisu do chmury.

Uruchamia go workflow „Uzupełnianie archiwum z MF" na zlecenie wykonawcy
w Dockerze (moduł Harmonogram): jeden podatek, jedno okno dat, jeden przebieg.
Wynik to pliki w katalogu wynik/ — GitHub zapisuje je jako artefakt, a wykonawca
wgrywa je prosto do bazy w Dockerze. Supabase nie bierze udziału: stare treści
nie zajmują miejsca w chmurze (limit 500 MB) i nie ma transferu.

OSTROŻNIE Z MF — PRIORYTET PONAD SZYBKOŚĆ
  Blokada adresów GitHuba zatrzymałaby też codzienną synchronizację, dlatego:
  - lista dokumentów idzie tą samą funkcją co w synchronizacji dziennej
    (pauza 1,5 s między stronami wyników);
  - treści pobieramy po jednej, z przerwą 8–12 s, nigdy równolegle
    (od 25.09.2026 wolniej: blokady przychodziły już po 59–400 treściach);
  - NIE pobieramy treści, które już mamy (ZNANE) — wcześniej każdy przebieg
    ściągał cały miesiąc od nowa, także duplikaty;
  - pierwsza oznaka blokady (429, 403, 5xx, brak odpowiedzi) kończy przebieg
    od razu i bez ponawiania — wykonawca wstrzymuje wtedy uzupełnianie na dobę;
  - najwyżej MAKS_DOK treści w przebiegu, liczone pełnymi dniami.

WEJŚCIE (zmienne środowiskowe)
  PODATEK   skrót, np. PIT
  PRZEPIS   ID ustawy w słowniku EUREKI; puste = wbudowany podatek (utils.KODY_PRZEPISOW)
  OD, DO    okno dat wydania, RRRR-MM-DD
  MAKS_DOK  najwięcej treści w przebiegu (1–1000)
  ZNANE     opcjonalnie: plik JSON z listą ID, które już mamy — pomijane bez
            pytania MF o treść

WYJŚCIE
  wynik/wynik.json         status, liczby, nastepne_od — od tej daty wykonawca
                           zleca kolejny przebieg tego podatku
  wynik/dokumenty.json.gz  wiersze tabeli dokumenty
"""

import gzip
import json
import os
import random
import re
import time
from datetime import date, timedelta

import requests

import utils

PRZERWA_MIN_S, PRZERWA_MAX_S = 8.0, 12.0
KATALOG = "wynik"


def _wejscie() -> tuple:
    podatek = os.environ.get("PODATEK", "").strip().upper()
    przepis = os.environ.get("PRZEPIS", "").strip()
    od = os.environ.get("OD", "").strip()
    do = os.environ.get("DO", "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,11}", podatek):
        raise SystemExit("Zły skrót podatku: %r" % podatek)
    kod = int(przepis) if przepis.isdigit() else utils.KODY_PRZEPISOW.get(podatek)
    if not kod:
        raise SystemExit("Brak ID ustawy dla podatku %s." % podatek)
    if date.fromisoformat(od) > date.fromisoformat(do):     # zła data = ValueError
        raise SystemExit("Okno %s..%s jest odwrócone." % (od, do))
    maks = max(1, min(int(os.environ.get("MAKS_DOK") or 500), 1000))
    return podatek, kod, od, do, maks


def _znane() -> set:
    plik = os.environ.get("ZNANE", "").strip()
    if not plik or not os.path.exists(plik):
        return set()
    with open(plik, encoding="utf-8") as f:
        return {str(x) for x in json.load(f)}


def _zapisz(wynik: dict, dokumenty: list) -> None:
    os.makedirs(KATALOG, exist_ok=True)
    with gzip.open(os.path.join(KATALOG, "dokumenty.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(dokumenty, f, ensure_ascii=False)
    with open(os.path.join(KATALOG, "wynik.json"), "w", encoding="utf-8") as f:
        json.dump(wynik, f, ensure_ascii=False, indent=2)
    print(json.dumps(wynik, ensure_ascii=False))


def main() -> None:
    podatek, kod, od, do, maks = _wejscie()
    wynik = {"podatek": podatek, "od": od, "do": do, "status": "OK",
             "lista": 0, "pobrane": 0, "brak_tresci": 0, "znane": 0,
             "nastepne_od": None}
    znane = _znane()
    dokumenty = []
    with requests.Session() as sesja:
        lista, status = utils.pobierz_wszystko_z_okresu(od, do, sesja, podatek, kod, log_fn=print)
        wynik["lista"] = len(lista)
        if status != "OK":
            # Niepełna lista też bywa znakiem, że MF zwalnia — treści nie ruszamy.
            wynik["status"] = "BLOKADA" if status == "ERROR" else "NIEPELNA_LISTA"
            wynik["nastepne_od"] = od
            return _zapisz(wynik, dokumenty)

        lista.sort(key=lambda d: (d["data"], d["id"]))
        poprzedni_dzien = None
        for nr, d in enumerate(lista, 1):
            # Limit sprawdzamy na granicy dni: dzień zawsze kończy się w całości,
            # więc następny przebieg zaczyna od czystego dnia i nic się nie gubi.
            if len(dokumenty) >= maks and d["data"] != poprzedni_dzien:
                wynik["status"], wynik["nastepne_od"] = "LIMIT", d["data"]
                break
            poprzedni_dzien = d["data"]
            if str(d["id"]) in znane:
                wynik["znane"] += 1
                continue
            time.sleep(random.uniform(PRZERWA_MIN_S, PRZERWA_MAX_S))
            tekst, st = utils.pobierz_tekst_pdf(d["id"], sesja=sesja)
            if st == "BLOKADA":
                # Ten dzień zacznie się od nowa; to, co już z niego mamy, pominie ON CONFLICT.
                wynik["status"], wynik["nastepne_od"] = "BLOKADA", d["data"]
                print("Oznaka blokady przy %s — koniec przebiegu, bez ponawiania." % d["sygnatura"])
                break
            if not tekst:
                wynik["brak_tresci"] += 1
                continue
            dokumenty.append({
                "id": d["id"], "sygnatura": d["sygnatura"], "podatek": podatek,
                "data_wyd": d["data"], "link": utils.PODGLAD_URL.format(id=d["id"]),
                "tekst": tekst, "format_zr": "HTML+PDF",
            })
            if nr % 25 == 0:
                print("  treści: %d/%d" % (nr, len(lista)))

    wynik["pobrane"] = len(dokumenty)
    if wynik["status"] == "OK":
        wynik["nastepne_od"] = (date.fromisoformat(do) + timedelta(days=1)).isoformat()
    _zapisz(wynik, dokumenty)


if __name__ == "__main__":
    main()
