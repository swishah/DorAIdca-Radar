#!/usr/bin/env python3
"""
synchronizacja_wyrokow.py — Cotygodniowa synchronizacja wyrokow sadow
administracyjnych (CBOSA) do bazy DorAIdca Radar. Uruchamiana przez GitHub Actions
w niedziele (oraz recznie: workflow_dispatch).

TRZY STRUMIENIE (sciezka C):
  1. METADANE (okno krotkie, dom. 10 dni): wszystkie nowe orzeczenia
     podatkowe — takze BEZ uzasadnienia. Baza od razu wie, ze wyrok
     istnieje (sentencja + metadane, status OCZEKUJE_NA_UZASADNIENIE).
  2. UZASADNIENIA (okno dlugie, dom. 180 dni, filtr "z uzasadnieniem"):
     dociaga pelne teksty do rekordow, ktore czekaly. Uzasadnienia
     publikowane sa czesto 2-3 miesiace po wyroku — stad dlugie okno.
  3. PRAWOMOCNOSC (okno dom. 15 miesiecy, filtr "prawomocne"):
     tylko listy wynikow (bez stron szczegolow) — oznacza w bazie
     rekordy, ktore w miedzyczasie sie uprawomocnily.

Po strumieniu 2: oznaczenie trwalych brakow (rekordy OCZEKUJACE starsze
niz ~9 miesiecy -> BEZ_UZASADNIENIA_TRWALE, nie beda juz rewizytowane).

TRYB KALIBRACJI (--kalibracja):
  Pierwsze uruchomienie na zywym CBOSA. Nie pisze NIC do bazy. Rozpoznaje
  formularz, wykonuje male zapytanie (VAT, 7 dni), pobiera 2 dokumenty
  i wypisuje wszystko, co sparsowal — do recznej weryfikacji, ze parser
  poprawnie czyta strukture strony, ZANIM zaczniemy zapisywac dane.

Zmienne srodowiskowe: SUPABASE_* oraz GMAIL_* jak w pozostalych skryptach.
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta

import db_core
import db_wyroki
import utils
import wyroki_cbosa as cbosa


# Ruchome okna wstecz. Wartosci domyslne opisuja normalny, cotygodniowy bieg:
# metadane 10 dni (z zapasem na jedna pominieta niedziele), uzasadnienia 180 dni
# (MF publikuje je 2-3 miesiace po wyroku), prawomocnosc 455 dni (~15 miesiecy).
#
# DLACZEGO ZE ZMIENNYCH SRODOWISKOWYCH, A NIE NA SZTYWNO
#   Gdy synchronizacja stoi dluzej niz okno metadanych, powstaje dziura, ktorej
#   zwykly przebieg NIGDY nie nadrobi: patrzy 10 dni wstecz, a brakuje tygodni.
#   Zdarzylo sie to realnie — zadanie padalo od 9 sierpnia do 6 wrzesnia przez
#   brakujaca zaleznosc (PyPDF2), czyli siedem tygodni bez danych. Nadrobienie
#   wymagalo wtedy edycji kodu, co jest zla droga dla operacji jednorazowej.
#   Teraz wystarczy jednorazowo podniesc okno zmienna srodowiskowa; domyslne
#   zachowanie pozostaje identyczne.
def _dni(nazwa: str, domyslnie: int) -> int:
    """Okno w dniach ze zmiennej srodowiskowej; bledna wartosc -> domyslna."""
    try:
        wartosc = int(os.environ.get(nazwa, "").strip() or domyslnie)
        return wartosc if wartosc > 0 else domyslnie
    except ValueError:
        return domyslnie


OKNO_METADANE_DNI     = _dni("WYROKI_OKNO_METADANE_DNI", 10)
OKNO_UZASADNIEN_DNI   = _dni("WYROKI_OKNO_UZASADNIEN_DNI", 180)
OKNO_PRAWOMOCNE_DNI   = _dni("WYROKI_OKNO_PRAWOMOCNE_DNI", 455)   # ~15 miesiecy


def _wczytaj_config_supabase() -> dict:
    return {
        "host":     os.environ["SUPABASE_HOST"],
        "port":     os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user":     os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode":  os.environ.get("SUPABASE_SSLMODE", "require"),
    }


def _okno(dni: int):
    dz = datetime.now()
    od = dz - timedelta(days=dni)
    return od.strftime("%Y-%m-%d"), dz.strftime("%Y-%m-%d")


def _okno_dla_podatku(od: str, do: str, podatek: str):
    """Przycina okno wyszukiwania do daty startowej podatku.

    Strumienie pracuja na ruchomych oknach wstecz — metadane 10 dni,
    uzasadnienia 180, prawomocnosc 455. Dla PCC (start 03.08.2026) oznaczaloby
    to zaciagniecie wyrokow sprzed wejscia podatku do systemu, czyli dokladnie
    tej historii, ktorej nie chcemy.

    Zwraca (od, do) albo None, gdy cale okno lezy przed data startowa —
    wtedy podatek pomijamy w tym przebiegu. Dla podatkow bez wlasnej daty
    startowej nie zmienia niczego.
    """
    prog = utils.data_start(podatek)
    if do < prog:
        return None
    return (max(od, prog), do)


# ---------------------------------------------------------------------------
# TRYB LEKKI — tylko sygnatury i metadane, BEZ pelnych tekstow
# ---------------------------------------------------------------------------
# Gdy WYROKI_TYLKO_METADANE=1 (domyslnie): zapisujemy sygnature + metadane
# (sad, data, podatek, symbole, hasla, wynik, prawomocnosc), ale POMIJAMY
# ciezkie pola sentencja/uzasadnienie i caly strumien uzasadnien. To niemal
# zeruje przyrost miejsca (metadane to kilkaset bajtow na wyrok), a dzieki
# upsertowi uzasadnienia mozna dociagnac pozniej: ustaw WYROKI_TYLKO_METADANE=0.
TYLKO_METADANE = (os.environ.get("WYROKI_TYLKO_METADANE", "1") == "1")


# ---------------------------------------------------------------------------
# STRUMIEN 1 — METADANE (wszystko nowe, takze bez uzasadnienia)
# ---------------------------------------------------------------------------
def strumien_metadane(db, sesja, formularz, log=print):
    od, do = _okno(OKNO_METADANE_DNI)
    log(f"\n=== STRUMIEN 1: METADANE | okno {od}..{do} ===")
    znane = db_wyroki.pobierz_id_wyrokow(db)
    bledy_wyszukiwania = 0

    for podatek, symbol in cbosa.SYMBOLE_PODATKOW.items():
        okno = _okno_dla_podatku(od, do, podatek)
        if okno is None:
            log(f"\n[{podatek}] pominiety — start podatku {utils.data_start(podatek)}")
            continue
        od_p, do_p = okno
        log(f"\n[{podatek}] symbol {symbol} | okno {od_p}..{do_p}")
        try:
            lista, total = cbosa.szukaj(sesja, formularz, symbol, od_p, do_p, log_fn=log)
        except cbosa.BladCBOSA as e:
            log(f"[{podatek}] BLAD wyszukiwania: {e}")
            db_wyroki.zapisz_historie_sync_wyrokow(db, "METADANE", od, do, podatek,
                                                    0, 0, 0, "ERROR", str(e))
            bledy_wyszukiwania += 1
            continue

        nowe_id = [i for i, _ in lista if i not in znane]
        log(f"[{podatek}] Znaleziono {len(lista)}"
            + (f" (CBOSA deklaruje {total})" if total is not None else "")
            + f", nowych do pobrania: {len(nowe_id)}")

        nowych, bledy, pominiete = 0, 0, 0
        for k, did in enumerate(nowe_id, 1):
            try:
                w = cbosa.pobierz_szczegoly(sesja, did, log_fn=None)
                # Zabezpieczenie 2. poziomu: filtr listy dziala po tytule;
                # gdyby postanowienie mialo nietypowy tytul i sie przeslizgnelo,
                # rodzaj z parsera szczegolow zatrzymuje je przed zapisem.
                if (w.get("rodzaj") or "").strip().lower().startswith("postanowienie"):
                    pominiete += 1
                    continue
                if TYLKO_METADANE:
                    # Tryb lekki: nie zapisujemy pelnych tekstow — tylko
                    # sygnatura + metadane. Uzasadnienia dojda przy WYROKI_TYLKO_METADANE=0.
                    w["sentencja"] = ""
                    w["uzasadnienie"] = ""
                    w["status_tresci"] = db_wyroki.STATUS_OCZEKUJE
                db_wyroki.zapisz_wyrok(db, w)
                nowych += 1
                if k % 10 == 0 or k == len(nowe_id):
                    log(f"[{podatek}] Postep szczegolow: {k}/{len(nowe_id)}")
            except Exception as e:
                bledy += 1
                log(f"[{podatek}] Blad dokumentu {did}: {e}")

        if pominiete:
            log(f"[{podatek}] Pominieto {pominiete} postanowien wykrytych na poziomie szczegolow.")
        status = "OK" if bledy == 0 else ("CZESCIOWO" if nowych else "ERROR")
        db_wyroki.zapisz_historie_sync_wyrokow(
            db, "METADANE", od, do, podatek, len(lista), nowych, 0, status,
            f"bledy dokumentow: {bledy}" if bledy else "")

    return bledy_wyszukiwania


# ---------------------------------------------------------------------------
# STRUMIEN 2 — UZASADNIENIA (dlugie okno, filtr "z uzasadnieniem")
# ---------------------------------------------------------------------------
def strumien_uzasadnienia(db, sesja, formularz, log=print):
    od, do = _okno(OKNO_UZASADNIEN_DNI)
    log(f"\n=== STRUMIEN 2: UZASADNIENIA | okno {od}..{do} (filtr: z uzasadnieniem) ===")
    znane = db_wyroki.pobierz_id_wyrokow(db)
    bledy_wyszukiwania = 0

    for podatek, symbol in cbosa.SYMBOLE_PODATKOW.items():
        okno = _okno_dla_podatku(od, do, podatek)
        if okno is None:
            log(f"\n[{podatek}] pominiety — start podatku {utils.data_start(podatek)}")
            continue
        od_p, do_p = okno
        log(f"\n[{podatek}] symbol {symbol} | okno {od_p}..{do_p}")
        try:
            lista, total = cbosa.szukaj(sesja, formularz, symbol, od_p, do_p,
                                        tylko_z_uzasadnieniem=True, log_fn=log)
        except cbosa.BladCBOSA as e:
            log(f"[{podatek}] BLAD wyszukiwania: {e}")
            db_wyroki.zapisz_historie_sync_wyrokow(db, "UZASADNIENIA", od, do, podatek,
                                                    0, 0, 0, "ERROR", str(e))
            bledy_wyszukiwania += 1
            continue

        # Rewizytujemy TYLKO to, co nie jest jeszcze KOMPLETNE w bazie —
        # dzieki temu cotygodniowy koszt to glownie listy, nie tysiace
        # stron szczegolow.
        do_pobrania = [i for i, _ in lista
                       if znane.get(i) != db_wyroki.STATUS_KOMPLETNY]
        log(f"[{podatek}] Z uzasadnieniem w oknie: {len(lista)}"
            + (f" (CBOSA: {total})" if total is not None else "")
            + f", wymaga pobrania/aktualizacji: {len(do_pobrania)}")

        nowych, zaktual, bledy = 0, 0, 0
        for k, did in enumerate(do_pobrania, 1):
            try:
                w = cbosa.pobierz_szczegoly(sesja, did, log_fn=None)
                if (w.get("rodzaj") or "").strip().lower().startswith("postanowienie"):
                    continue   # zabezpieczenie 2. poziomu — jak w strumieniu 1
                wynik = db_wyroki.zapisz_wyrok(db, w)
                if wynik == "NOWY":
                    nowych += 1
                else:
                    zaktual += 1
                if k % 10 == 0 or k == len(do_pobrania):
                    log(f"[{podatek}] Postep: {k}/{len(do_pobrania)}")
            except Exception as e:
                bledy += 1
                log(f"[{podatek}] Blad dokumentu {did}: {e}")

        status = "OK" if bledy == 0 else ("CZESCIOWO" if (nowych + zaktual) else "ERROR")
        db_wyroki.zapisz_historie_sync_wyrokow(
            db, "UZASADNIENIA", od, do, podatek, len(lista), nowych, zaktual, status,
            f"bledy dokumentow: {bledy}" if bledy else "")

    trwale = db_wyroki.oznacz_trwale_braki(db)
    if trwale:
        log(f"\nOznaczono {trwale} rekordow jako BEZ_UZASADNIENIA_TRWALE "
            f"(starsze niz {db_wyroki.DNI_DO_TRWALEGO_BRAKU} dni, nadal bez uzasadnienia).")
    return bledy_wyszukiwania


# ---------------------------------------------------------------------------
# STRUMIEN 3 — PRAWOMOCNOSC (tylko listy, zero stron szczegolow)
# ---------------------------------------------------------------------------
def strumien_prawomocnosc(db, sesja, formularz, log=print):
    od, do = _okno(OKNO_PRAWOMOCNE_DNI)
    log(f"\n=== STRUMIEN 3: PRAWOMOCNOSC | okno {od}..{do} (filtr: prawomocne, tylko listy) ===")
    bledy_wyszukiwania = 0

    for podatek, symbol in cbosa.SYMBOLE_PODATKOW.items():
        okno = _okno_dla_podatku(od, do, podatek)
        if okno is None:
            log(f"\n[{podatek}] pominiety — start podatku {utils.data_start(podatek)}")
            continue
        od_p, do_p = okno
        try:
            lista, total = cbosa.szukaj(sesja, formularz, symbol, od_p, do_p,
                                        tylko_prawomocne=True, log_fn=log)
        except cbosa.BladCBOSA as e:
            log(f"[{podatek}] BLAD wyszukiwania: {e}")
            db_wyroki.zapisz_historie_sync_wyrokow(db, "PRAWOMOCNOSC", od, do, podatek,
                                                    0, 0, 0, "ERROR", str(e))
            bledy_wyszukiwania += 1
            continue
        ids = [i for i, _ in lista]
        zmienione = db_wyroki.oznacz_prawomocne(db, ids)
        log(f"[{podatek}] Prawomocnych w oknie: {len(ids)}, nowo oznaczonych w bazie: {zmienione}")
        db_wyroki.zapisz_historie_sync_wyrokow(
            db, "PRAWOMOCNOSC", od, do, podatek, len(ids), 0, zmienione, "OK", "")

    return bledy_wyszukiwania


# ---------------------------------------------------------------------------
# KALIBRACJA — pierwsze uruchomienie, zero zapisu do bazy
# ---------------------------------------------------------------------------
def kalibracja(log=print):
    log("=" * 70)
    log("TRYB KALIBRACJI — nic nie zapisuje do bazy")
    log("=" * 70)
    sesja = cbosa.nowa_sesja()

    log("\n1) Rozpoznawanie formularza...")
    formularz = cbosa.poznaj_formularz(sesja, log_fn=log)
    log("   Klucze pomocy z polami: " + ", ".join(sorted(formularz["pola"].keys())))
    for klucz in ("symbole", "data_orzeczenia", "z_uzasadnieniem", "s_prawomocne"):
        pola = formularz["pola"].get(klucz, [])
        log(f"   - {klucz}: " + (", ".join(f"{p['name']}({p['type']})" for p in pola) or "NIE ZNALEZIONO!"))

    od, do = _okno(7)
    log(f"\n2) Testowe zapytanie: VAT (6110), okno {od}..{do}...")
    lista, total = cbosa.szukaj(sesja, formularz, "6110", od, do, log_fn=log)
    log(f"   Wynikow: {len(lista)}" + (f", CBOSA deklaruje: {total}" if total is not None else ", (nie odczytano licznika)"))
    for did, tytul in lista[:5]:
        log(f"   - {did}: {tytul}")

    if lista:
        log("\n3) Parsowanie 2 przykladowych dokumentow...")
        for did, _ in lista[:2]:
            w = cbosa.pobierz_szczegoly(sesja, did, log_fn=log)
            log(f"\n   /doc/{did}:")
            for pole in ("sygnatura", "rodzaj", "sad", "data_orzeczenia", "podatek",
                          "symbole", "prawomocny", "status_tresci", "tresc_wyniku"):
                log(f"     {pole}: {w.get(pole)}")
            log(f"     sentencja: {len(w.get('sentencja',''))} znakow | "
                f"uzasadnienie: {len(w.get('uzasadnienie',''))} znakow")
            log(f"     poczatek sentencji: {w.get('sentencja','')[:180]}")
    log("\nKALIBRACJA ZAKONCZONA. Sprawdz powyzsze dane — jesli sygnatury, daty,")
    log("podatek i dlugosci tekstow wygladaja sensownie, mozna uruchomic tryb pelny.")


MAKS_PROB_CALEGO_SYNC   = 3
ODSTEP_MIEDZY_PROBAMI_S = 600   # 10 minut — czas na "wyleczenie" awarii serwera


def _wykonaj_probe(db, wybrane, log=print) -> int:
    """
    Jedna pelna proba: NOWA sesja + swiezo rozpoznany formularz + wybrane
    strumienie. Zwraca laczna liczbe bledow wyszukiwania (0 = czysto).
    Nowa sesja przy kazdej probie jest kluczowa: po RemoteDisconnected
    stare polaczenie/cookies moga byc bezuzyteczne.
    """
    sesja = cbosa.nowa_sesja()
    formularz = cbosa.poznaj_formularz(sesja, log_fn=log)

    bledy = 0
    if "1" in wybrane:
        bledy += strumien_metadane(db, sesja, formularz) or 0
    if "2" in wybrane:
        bledy += strumien_uzasadnienia(db, sesja, formularz) or 0
    if "3" in wybrane:
        bledy += strumien_prawomocnosc(db, sesja, formularz) or 0
    return bledy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kalibracja", action="store_true",
                        help="Tylko test parsera na zywym CBOSA, bez zapisu do bazy")
    parser.add_argument("--strumienie", default="1,2,3",
                        help="Ktore strumienie uruchomic, np. '1,2' (dom. wszystkie)")
    args = parser.parse_args()

    if args.kalibracja:
        kalibracja()
        return

    print("=" * 70)
    print("DorAIdca Radar — Synchronizacja Wyrokow CBOSA")
    print(f"Okna wstecz: metadane {OKNO_METADANE_DNI} dni, "
          f"uzasadnienia {OKNO_UZASADNIEN_DNI}, prawomocnosc {OKNO_PRAWOMOCNE_DNI}."
          + ("  [NIESTANDARDOWE — nadrabianie zaleglosci]"
             if (OKNO_METADANE_DNI, OKNO_UZASADNIEN_DNI, OKNO_PRAWOMOCNE_DNI)
                != (10, 180, 455) else ""))
    print("=" * 70)

    db = db_core.SupabaseDB(_wczytaj_config_supabase())
    db_wyroki.inicjalizuj_schemat_wyrokow(db)
    print("Polaczenie z Supabase OK, schemat wyrokow gotowy.")

    wybrane = {s.strip() for s in args.strumienie.split(",")}
    if TYLKO_METADANE:
        # Bez pobierania uzasadnien (strumien 2). Metadane (1) + prawomocnosc (3)
        # zostaja; oznaczanie trwalych brakow tez pomijamy (dzieje sie w str. 2),
        # zeby po powrocie do pelnego trybu wszystkie rekordy dostaly uzasadnienia.
        wybrane.discard("2")
        print("TRYB LEKKI (WYROKI_TYLKO_METADANE=1): zapisuje tylko sygnatury "
              "i metadane, bez uzasadnien. Ustaw WYROKI_TYLKO_METADANE=0, aby "
              "dociagnac pelne teksty.")

    # ── ZEWNETRZNA PETLA PONAWIANIA CALEGO PRZEBIEGU ────────────────────────
    # Strumienie sa idempotentne (upsert + pomijanie znanych id), wiec
    # powtorzenie calosci jest bezpieczne: wykonana praca nie zdubluje sie,
    # a przy ponowieniu zostanie pominieta. Chwilowa awaria CBOSA (np.
    # RemoteDisconnected przy rozpoznawaniu formularza) nie ubija juz
    # calego wielogodzinnego przebiegu.
    ostatni_blad = None
    for proba in range(1, MAKS_PROB_CALEGO_SYNC + 1):
        print(f"\n{'='*70}\nPROBA {proba}/{MAKS_PROB_CALEGO_SYNC}\n{'='*70}")
        try:
            bledy = _wykonaj_probe(db, wybrane)
            if bledy == 0:
                print(f"\nProba {proba}: zakonczona czysto.")
                ostatni_blad = None
                break
            ostatni_blad = f"{bledy} bledow wyszukiwania"
            print(f"\nProba {proba}: {ostatni_blad}.")
        except (cbosa.BladCBOSA, Exception) as e:
            ostatni_blad = str(e)
            print(f"\nProba {proba} przerwana bledem: {e}")

        if proba < MAKS_PROB_CALEGO_SYNC:
            print(f"Czekam {ODSTEP_MIEDZY_PROBAMI_S}s przed kolejna proba "
                  f"(czas na odzyskanie dostepnosci CBOSA)...")
            time.sleep(ODSTEP_MIEDZY_PROBAMI_S)
        else:
            print(f"Wyczerpano {MAKS_PROB_CALEGO_SYNC} prob.")

    print("\n" + "=" * 70)
    try:
        stats = db_wyroki.statystyki_wyrokow(db)
        print(f"PODSUMOWANIE: {stats['total']} wyrokow w bazie")
        for p in stats["per_podatek"]:
            print(f"  {p['podatek'] or '(bez podatku)'}: {p['liczba']} "
                  f"(kompletne: {p['kompletne']}, oczekujace: {p['oczekujace']}, "
                  f"prawomocne: {p['prawomocne']})")
    except Exception as e:
        print(f"Nie udalo sie pobrac statystyk: {e}")
    print("=" * 70)

    if ostatni_blad:
        print(f"ZAKONCZONO Z BLEDAMI: {ostatni_blad}")
        sys.exit(1)


if __name__ == "__main__":
    main()
