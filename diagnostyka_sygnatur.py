# -*- coding: utf-8 -*-
"""
diagnostyka_sygnatur.py — sprawdzenie normalizacji na PRAWDZIWYCH danych.

PO CO
  Testy offline dowodzą, że Python i JS liczą to samo. Nie dowodzą, że wynik
  pasuje do Twojej bazy — bo Twojej bazy nie widzę. Ten skrypt uruchamiasz
  u siebie; odpowiada na trzy pytania:

    1. Czy normalizacja czegoś nie SKLEJA? (dwie różne sygnatury -> jeden
       klucz oznaczałoby, że wtyczka pokaże streszczenie nie tego dokumentu)
    2. Czy któraś sygnatura nie znika po normalizacji do pustego łańcucha?
    3. Jak wygląda rozkład rodzajów i ile sygnatur nie pasuje do żadnego wzorca?

  Dodatkowo tryb --sprawdz pozwala wkleić sygnaturę SKOPIOWANĄ Z LEX-a
  i sprawdzić, czy trafia w bazę. To jest właściwy test końcowy fazy 0.

EGRESS
  Pobieramy WYŁĄCZNIE kolumnę `sygnatura` (i `id`). Żadnych pełnych tekstów —
  przy kilkudziesięciu tysiącach dokumentów to i tak kilka megabajtów.

URUCHOMIENIE
  export SUPABASE_DB_URL="postgresql://user:haslo@host:5432/postgres"
  python3 diagnostyka_sygnatur.py
  python3 diagnostyka_sygnatur.py --sprawdz "0114-KDIP2-2.4010.123.2026.1.AS"
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from collections import Counter, defaultdict

import db_core
from normalizacja_sygnatur import (
    normalizuj_sygnature,
    klucz_bez_inicjalow,
    rodzaj_sygnatury,
    podejrzany_klucz,
)

# Tabele do przejrzenia: (nazwa_tabeli, kolumna_id, kolumna_sygnatury, etykieta).
# Wyroki siedzą w osobnej tabeli — jeśli nazwa u Ciebie jest inna, popraw tutaj.
ZRODLA = [
    ("dokumenty", "id", "sygnatura", "interpretacje"),
    ("wyroki",    "id", "sygnatura", "wyroki"),
]


def _polacz() -> db_core.SupabaseDB:
    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return db_core.SupabaseDB({"url": url})
    braki = [k for k in ("SUPABASE_HOST", "SUPABASE_USER", "SUPABASE_PASSWORD")
             if not os.environ.get(k)]
    if braki:
        raise SystemExit("Brak konfiguracji bazy. Ustaw SUPABASE_DB_URL albo "
                         + ", ".join(braki) + ".")
    return db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode": os.environ.get("SUPABASE_SSLMODE", "require"),
    })


def _wczytaj(db, tabela: str, kol_id: str, kol_syg: str) -> list[dict]:
    """Zwraca [] gdy tabela nie istnieje — żeby brak modułu wyroków nie
    wywracał całej diagnostyki."""
    try:
        return db.wykonaj(
            f"SELECT {kol_id} AS id, {kol_syg} AS sygnatura FROM {tabela}",
            fetch=True)
    except Exception as e:
        print(f"  (pomijam tabelę {tabela}: {str(e).splitlines()[0][:90]})")
        return []


def _ukryte_znaki(s: str) -> list[str]:
    """Nazwy znaków niewidocznych / nietypowych obecnych w łańcuchu."""
    znalezione = []
    for ch in s:
        if ch in "\u00ad\u200b\u200c\u200d\u2060\ufeff\u00a0":
            znalezione.append(unicodedata.name(ch, f"U+{ord(ch):04X}"))
        elif ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
            znalezione.append(unicodedata.name(ch, f"U+{ord(ch):04X}"))
    return sorted(set(znalezione))


# ---------------------------------------------------------------------------
# RAPORT GŁÓWNY
# ---------------------------------------------------------------------------
def raport(db) -> int:
    print("=" * 78)
    print("DIAGNOSTYKA NORMALIZACJI SYGNATUR — dane produkcyjne")
    print("=" * 78)

    wszystkie: list[tuple[str, str, str]] = []   # (zrodlo, id, sygnatura_surowa)
    for tabela, kol_id, kol_syg, etykieta in ZRODLA:
        rows = _wczytaj(db, tabela, kol_id, kol_syg)
        if rows:
            print(f"  {etykieta:<16} {len(rows):>7} rekordów  (tabela {tabela})")
        wszystkie += [(etykieta, str(r["id"]), r.get("sygnatura") or "")
                      for r in rows]

    if not wszystkie:
        print("\nBrak danych do sprawdzenia — żadna z tabel nie odpowiedziała.")
        return 1

    print(f"\n  RAZEM             {len(wszystkie):>7} sygnatur\n")

    # ── normalizacja całości ──
    wg_klucza: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    puste, rodzaje, zmienione, z_ukrytymi = [], Counter(), 0, []

    for zrodlo, ident, surowa in wszystkie:
        klucz = normalizuj_sygnature(surowa)

        # Sygnatury znikające raportujemy OSOBNO (sekcja 2), a nie jako
        # „NIEZNANY” — inaczej zawyżałyby rozkład rodzajów i wyglądałyby na
        # problem z rozpoznawaniem formatu, którym nie są.
        if not klucz:
            puste.append((zrodlo, ident, surowa))
            continue

        rodzaje[rodzaj_sygnatury(klucz)] += 1

        wg_klucza[klucz].append((zrodlo, ident, surowa))
        if klucz != (surowa or "").strip():
            zmienione += 1
        ukryte = _ukryte_znaki(surowa)
        if ukryte:
            z_ukrytymi.append((zrodlo, ident, surowa, ukryte))

    # ── 1. KOLIZJE — najgroźniejsze ──
    print("-" * 78)
    print("1. KOLIZJE (różne sygnatury sprowadzone do jednego klucza)")
    print("-" * 78)
    kolizje = {k: v for k, v in wg_klucza.items()
               if len({s for _, _, s in v}) > 1}
    if not kolizje:
        print("  Brak kolizji. Normalizacja nie skleja różnych dokumentów.\n")
    else:
        print(f"  ZNALEZIONO {len(kolizje)} kolizji — WYMAGAJĄ OCENY:\n")
        for klucz, poz in list(kolizje.items())[:20]:
            print(f"  klucz: {klucz}")
            for zrodlo, ident, surowa in poz:
                print(f"     [{zrodlo}] {ident}  <-  {surowa!r}")
            print()
        if len(kolizje) > 20:
            print(f"  ... i {len(kolizje) - 20} dalszych.\n")
        print("  UWAGA: część kolizji może być prawidłowa (ten sam dokument")
        print("  zapisany dwa razy). Kolizja między RÓŻNYMI dokumentami to")
        print("  błąd blokujący — normalizacja jest wtedy za agresywna.\n")

    # ── 2. PUSTE ──
    print("-" * 78)
    print("2. SYGNATURY ZNIKAJĄCE PO NORMALIZACJI")
    print("-" * 78)
    if not puste:
        print("  Brak. Każda sygnatura dała niepusty klucz.\n")
    else:
        print(f"  ZNALEZIONO {len(puste)}:\n")
        for zrodlo, ident, surowa in puste[:15]:
            print(f"     [{zrodlo}] {ident}  <-  {surowa!r}")
        print()

    # ── 3. ROZKŁAD RODZAJÓW ──
    print("-" * 78)
    print("3. ROZPOZNANE RODZAJE")
    print("-" * 78)
    razem_rodzajow = sum(rodzaje.values()) or 1
    for r, n in rodzaje.most_common():
        udzial = 100.0 * n / razem_rodzajow
        print(f"  {r:<10} {n:>7}  ({udzial:5.1f}%)")
    nieznane = rodzaje.get("NIEZNANY", 0)
    if nieznane:
        print(f"\n  {nieznane} sygnatur nie pasuje do żadnego wzorca. To nie")
        print("  blokuje dopasowania (działa na pełnym kluczu), ale warto")
        print("  zobaczyć próbkę — może ujawnić format, o którym nie wiem:\n")
        pokazane = 0
        for zrodlo, ident, surowa in wszystkie:
            k = normalizuj_sygnature(surowa)
            if k and rodzaj_sygnatury(k) == "NIEZNANY":
                print(f"     [{zrodlo}] {surowa!r}  ->  {k}")
                pokazane += 1
                if pokazane >= 15:
                    break
        if not pokazane:
            print("     (brak przykładów do pokazania)")
    print()

    # ── 4. UKRYTE ZNAKI W SAMEJ BAZIE ──
    print("-" * 78)
    print("4. ZNAKI NIEWIDOCZNE / NIETYPOWE W BAZIE")
    print("-" * 78)
    if not z_ukrytymi:
        print("  Brak. Sygnatury w bazie są czyste — problem może więc")
        print("  wystąpić tylko po stronie LEX-a, nie po Twojej.\n")
    else:
        print(f"  ZNALEZIONO {len(z_ukrytymi)} sygnatur ze znakami specjalnymi:\n")
        for zrodlo, ident, surowa, ukryte in z_ukrytymi[:15]:
            print(f"     [{zrodlo}] {surowa!r}")
            print(f"        {', '.join(ukryte)}")
        print()

    # ── 5. PODSUMOWANIE ──
    print("-" * 78)
    print("5. PODSUMOWANIE")
    print("-" * 78)
    print(f"  Kluczy unikalnych:              {len(wg_klucza)}")
    print(f"  Sygnatur zmienionych normalizacją: {zmienione}")

    bez_ini = defaultdict(set)
    for klucz in wg_klucza:
        bez_ini[klucz_bez_inicjalow(klucz)].add(klucz)
    wielo = {k: v for k, v in bez_ini.items() if len(v) > 1}
    print(f"  Kluczy dzielących postać bez inicjałów: {len(wielo)}")
    if wielo:
        print("     (to normalne: sprostowania tego samego pisma. Dlatego klucz")
        print("      bez inicjałów jest wyłącznie DRUGIM wyborem przy szukaniu.)")

    blokujace = bool(kolizje) or bool(puste)
    print()
    if blokujace:
        print("  WYNIK: są sprawy do rozstrzygnięcia — patrz sekcje 1 i 2.")
        print("  Prześlij mi ten raport, zanim przejdziemy do fazy 1.")
    else:
        print("  WYNIK: czysto. Normalizacja jest bezpieczna dla Twoich danych.")
        print("  Zostaje sprawdzić drugą stronę — patrz tryb --sprawdz poniżej.")
    print()
    print("  KROK NASTĘPNY: otwórz kilka dokumentów w LEX, skopiuj stamtąd")
    print("  sygnatury i uruchom dla każdej:")
    print('     python3 diagnostyka_sygnatur.py --sprawdz "<wklejona sygnatura>"')
    print("  Dopiero to potwierdza, że LEX trafia w Twoją bazę.")
    return 1 if blokujace else 0


# ---------------------------------------------------------------------------
# TRYB SPRAWDZANIA POJEDYNCZEJ SYGNATURY
# ---------------------------------------------------------------------------
def sprawdz(db, surowa: str) -> int:
    print("=" * 78)
    print("SPRAWDZENIE POJEDYNCZEJ SYGNATURY")
    print("=" * 78)
    print(f"  Wejście (dokładnie jak wklejone): {surowa!r}")

    ukryte = _ukryte_znaki(surowa)
    if ukryte:
        print(f"  Znaki niewidoczne w źródle: {', '.join(ukryte)}")
        print("  (to dokładnie ten przypadek, dla którego powstał ten moduł)")

    klucz = normalizuj_sygnature(surowa)
    print(f"  Klucz kanoniczny: {klucz}")
    print(f"  Rozpoznany rodzaj: {rodzaj_sygnatury(klucz)}")
    print(f"  Klucz bez inicjałów: {klucz_bez_inicjalow(klucz)}")

    ostrzezenie = podejrzany_klucz(klucz)
    if ostrzezenie:
        print()
        print(f"  >>> UWAGA: {ostrzezenie}")
        print("  >>> Sprawdź, czy nie wkleiłeś dwóch sygnatur bez separatora |")
        print("  >>> albo sygnatury razem z fragmentem otaczającego tekstu.")
    print()

    if not klucz:
        print("  Sygnatura znika po normalizacji — nie ma czego szukać.")
        return 1

    trafienia = []
    for tabela, kol_id, kol_syg, etykieta in ZRODLA:
        for r in _wczytaj(db, tabela, kol_id, kol_syg):
            k = normalizuj_sygnature(r.get("sygnatura") or "")
            if k == klucz:
                trafienia.append((etykieta, "pełne", r["id"], r["sygnatura"]))
            elif k and klucz_bez_inicjalow(k) == klucz_bez_inicjalow(klucz):
                trafienia.append((etykieta, "bez inicjałów", r["id"], r["sygnatura"]))

    print("-" * 78)
    if not trafienia:
        print("  BRAK TRAFIEŃ.")
        print("  Możliwe przyczyny: dokumentu nie ma w archiwum (np. wydany")
        print("  przed progiem synchronizacji), albo LEX zapisuje sygnaturę")
        print("  w formacie, którego normalizacja nie sprowadza do tej samej")
        print("  postaci. Żeby to rozstrzygnąć, uruchom raport główny i")
        print("  poszukaj tej sygnatury w sekcji 3.")
        return 1

    pelne = [t for t in trafienia if t[1] == "pełne"]
    print(f"  TRAFIENIA: {len(trafienia)}  (pełnych: {len(pelne)})\n")
    for etykieta, typ, ident, surowa_db in trafienia[:10]:
        print(f"     [{etykieta}] dopasowanie {typ}")
        print(f"        id w bazie: {ident}")
        print(f"        zapis w bazie: {surowa_db!r}")
    print()
    if pelne:
        print("  WYNIK: dopasowanie pełne — wtyczka znajdzie ten dokument.")
        return 0
    print("  WYNIK: tylko dopasowanie przybliżone (bez inicjałów).")
    print("  Wtyczka pokaże je z wyraźnym oznaczeniem niepewności.")
    return 0


def sprawdz_wiele(db, wejscie: str) -> int:
    """Sprawdza kilka sygnatur naraz. Rozdzielaj znakiem | albo nową linią —
    sygnatury zawierają ukośniki, kropki i myślniki, więc pionowa kreska jest
    jedynym separatorem, który na pewno w nich nie wystąpi."""
    czesci = [s.strip() for s in wejscie.replace("\n", "|").split("|")]
    czesci = [s for s in czesci if s]

    if not czesci:
        print("Nie podano żadnej sygnatury.")
        return 1
    if len(czesci) == 1:
        return sprawdz(db, czesci[0])

    wyniki = []
    for i, s in enumerate(czesci, 1):
        print(f"\n\n########## {i} z {len(czesci)} ##########")
        kod = sprawdz(db, s)
        wyniki.append((s, kod))

    print("\n\n" + "=" * 78)
    print("PODSUMOWANIE ZBIORCZE")
    print("=" * 78)
    trafione = sum(1 for _, k in wyniki if k == 0)
    for s, kod in wyniki:
        status = "ZNALEZIONO   " if kod == 0 else "brak trafień "
        skrot = s if len(s) <= 46 else s[:43] + "..."
        print(f"  {status} {skrot}")
    print(f"\n  Znalezionych: {trafione} z {len(wyniki)}")
    print()
    print("  Brak trafienia jest POPRAWNY dla dokumentów, których nie ma")
    print("  w archiwum (np. wydanych przed progiem synchronizacji). Problemem")
    print("  jest wyłącznie sytuacja, w której dokument w archiwum JEST,")
    print("  a mimo to nie został znaleziony.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnostyka normalizacji sygnatur.")
    ap.add_argument("--sprawdz", metavar="SYGNATURY",
                    help="jedna sygnatura albo kilka rozdzielonych znakiem |")
    args = ap.parse_args()

    db = _polacz()
    return sprawdz_wiele(db, args.sprawdz) if args.sprawdz else raport(db)


if __name__ == "__main__":
    sys.exit(main())
