#!/usr/bin/env python3
"""
raport_na_zadanie.py — Pobiera interpretacje na zadanie dla wybranego
roku/miesiaca/podatku i zapisuje je w bazie Supabase. NIE generuje pliku
Word — celem jest wylacznie zasilenie bazy danymi. Po zakonczeniu wysyla
powiadomienie mailowe (bez zalacznika) z podsumowaniem.

Uruchamiany recznie przez GitHub Actions (workflow_dispatch / repository_dispatch)
na zadanie uzytkownika z aplikacji Streamlit ("Sciagacz Interpretacji" -> Pobieranie
na zadanie -> tryb "w tle").

ODPORNOSC NA AWARIE API MF:
  1. Kazde pojedyncze zapytanie HTTP ma wbudowany retry (3x, w utils.py)
  2. Po pobraniu - druga, niezalezna weryfikacja liczby dokumentow w MF
     (samoleczaca: jesli wykryje brak, dociaga automatycznie)
  3. Jesli caly podatek zakonczy sie bledem (np. dluzsza awaria MF) -
     CALY SKRYPT probuje ponownie po przerwie (do 3 razy), zanim odda
     kontrole z bledem

Wymagane zmienne srodowiskowe:
  SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD
  GMAIL_ADRES, GMAIL_HASLO_APLIKACJI, EMAIL_ODBIORCA

Parametry (argumenty CLI):
  --rok       np. 2026
  --miesiac   np. 1 (styczen) .. 12 (grudzien)
  --podatek   PIT | CIT | VAT | AKCYZA | PCC | WSZYSTKIE

Przyklad:
  python raport_na_zadanie.py --rok 2026 --miesiac 4 --podatek CIT
  python raport_na_zadanie.py --rok 2026 --miesiac 2 --podatek WSZYSTKIE
"""

import os
import sys
import time
import argparse

import db_core
import raport_silnik as silnik


MAKS_PROB_CALEGO_RAPORTU = 3
ODSTEP_MIEDZY_PROBAMI_S  = 600  # 10 minut


def _wczytaj_config_supabase() -> dict:
    url = os.environ.get("SUPABASE_URL", "")
    if url:
        return {"url": url}
    return {
        "host":     os.environ["SUPABASE_HOST"],
        "port":     os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user":     os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode":  os.environ.get("SUPABASE_SSLMODE", "require"),
    }


def _czy_wymaga_ponowienia(wyniki: list) -> bool:
    """
    Decyduje czy CALE pobieranie powinno byc powtorzone.
    Powtarzamy jesli ktorykolwiek podatek ma status wskazujacy na
    problem z dostepnoscia MF (nie na brak dokumentow - to normalne).
    """
    statusy_wymagajace_retry = {"ERROR", "BLOKADA", "WERYFIKACJA_NIEUDANA"}
    return any(w["status"] in statusy_wymagajace_retry for w in wyniki)


def _wykonaj_probe(db, podatki_do_przetworzenia, data_od, data_do, opis_okresu, numer_proby) -> list:
    """Wykonuje jedna pelna probe pobrania dla wszystkich wybranych podatkow. Bez generowania pliku."""
    wyniki = []
    for pod in podatki_do_przetworzenia:
        print(f"\n--- {pod} (proba {numer_proby}) ---")
        wynik = silnik.generuj_raport_dla_podatku(
            db, pod, data_od, data_do, opis_okresu, log_fn=print, generuj_plik=False
        )
        wyniki.append(wynik)
        wer_info = ""
        if wynik.get("weryfikacja"):
            wer_info = f" | weryfikacja: {wynik['weryfikacja']['status']}"
        print(f"[{pod}] Status: {wynik['status']} | Dokumentow: {wynik['liczba_dok']}{wer_info}")
    return wyniki


def main(rok: int, miesiac: int, podatek: str):
    print("=" * 70)
    print(f"DorAIdca Radar — Sciagacz Interpretacji — pobieranie na zadanie: {podatek} / {miesiac:02d}.{rok}")
    print("=" * 70)

    data_od, data_do, opis_okresu = silnik.zakres_z_roku_miesiaca(rok, miesiac)
    print(f"Zakres: {data_od.date()} — {data_do.date()} ({opis_okresu})")

    config = _wczytaj_config_supabase()
    db = db_core.SupabaseDB(config)
    db.inicjalizuj_schemat()
    print("Polaczenie z Supabase OK.")

    podatki_do_przetworzenia = (
        silnik.PODATKI_WSZYSTKIE if podatek == "WSZYSTKIE" else [podatek]
    )

    # ── GLOWNA PETLA Z AUTOMATYCZNYM RETRY CALEGO POBIERANIA ───────────────
    wyniki = None
    proba = 1
    for proba in range(1, MAKS_PROB_CALEGO_RAPORTU + 1):
        wyniki = _wykonaj_probe(db, podatki_do_przetworzenia, data_od, data_do, opis_okresu, proba)

        if not _czy_wymaga_ponowienia(wyniki):
            print(f"\nProba {proba}: wszystko OK, konczy petle retry.")
            break

        if proba < MAKS_PROB_CALEGO_RAPORTU:
            print(
                f"\nProba {proba}/{MAKS_PROB_CALEGO_RAPORTU} wykryla problemy z dostepnoscia MF. "
                f"Czekam {ODSTEP_MIEDZY_PROBAMI_S}s przed kolejna proba..."
            )
            time.sleep(ODSTEP_MIEDZY_PROBAMI_S)
        else:
            print(
                f"\nWyczerpano {MAKS_PROB_CALEGO_RAPORTU} prob. "
                "Wysylam powiadomienie z tym co udalo sie zebrac."
            )

    # ── POWIADOMIENIE MAILOWE (bez zalacznika) ──────────────────────────────
    gmail_adres = os.environ.get("GMAIL_ADRES")
    gmail_haslo = os.environ.get("GMAIL_HASLO_APLIKACJI")
    odbiorca    = os.environ.get("EMAIL_ODBIORCA", gmail_adres)

    if not gmail_adres or not gmail_haslo:
        print("\nBrak konfiguracji email — pomijam powiadomienie.")
    else:
        silnik.wyslij_email_powiadomienie_pobrania(
            wyniki, opis_okresu, gmail_adres, gmail_haslo, odbiorca, log_fn=print,
        )

    print("\n" + "=" * 70)
    print("PODSUMOWANIE KONCOWE:")
    for w in wyniki:
        wer = w.get("weryfikacja")
        wer_str = f" | weryfikacja: {wer['status']}" if wer else ""
        print(f"  {w['podatek']}: {w['liczba_dok']} dokumentow (status: {w['status']}){wer_str}")
    print("=" * 70)

    # ── ZAPIS HISTORII DO BAZY (widoczne potem w Streamlit) ────────────────
    try:
        liczba_dok_lacznie = sum(w["liczba_dok"] for w in wyniki)
        statusy = [w["status"] for w in wyniki]
        if "ERROR" in statusy:
            status_koncowy = "ERROR"
        elif "NIEZGODNOSC" in statusy:
            status_koncowy = "NIEZGODNOSC"
        elif "WERYFIKACJA_NIEUDANA" in statusy:
            status_koncowy = "WERYFIKACJA_NIEUDANA"
        else:
            status_koncowy = "OK"

        szczegoly_lista = []
        for w in wyniki:
            wer = w.get("weryfikacja")
            if wer and wer["status"] == "NIEZGODNOSC":
                szczegoly_lista.append(
                    f"{w['podatek']}: MF={wer['liczba_w_mf']}, archiwum={wer['liczba_w_archiwum']}"
                )
        szczegoly_str = "; ".join(szczegoly_lista) if szczegoly_lista else ""

        db_core.zapisz_historie_raportu(
            db, rok=rok, miesiac=miesiac, podatek=podatek,
            liczba_dok=liczba_dok_lacznie, liczba_prob=proba,
            status=status_koncowy, szczegoly=szczegoly_str,
        )
        print(f"Zapisano historie: status={status_koncowy}, prob={proba}")
    except Exception as e:
        print(f"OSTRZEZENIE: nie udalo sie zapisac historii: {e}")

    bledy_krytyczne = [w for w in wyniki if w["status"] == "ERROR"]
    if bledy_krytyczne:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rok", type=int, required=True)
    parser.add_argument("--miesiac", type=int, required=True, choices=range(1, 13))
    parser.add_argument(
        "--podatek", type=str, required=True,
        choices=["PIT", "CIT", "VAT", "AKCYZA", "PCC", "WSZYSTKIE"]
    )
    args = parser.parse_args()
    main(rok=args.rok, miesiac=args.miesiac, podatek=args.podatek)
