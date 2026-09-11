#!/usr/bin/env python3
"""
synchronizacja_dzienna.py — Codzienna automatyczna synchronizacja bazy
interpretacji indywidualnych. Uruchamiany przez GitHub Actions codziennie
o 3:00 w nocy.

Co robi:
  1. Wyznacza ruchome okno ostatnich 10 dni (patrz OKNO_SYNCHRONIZACJI_DNI
     w raport_silnik.py).
  2. Dla kazdego podatku (PIT, CIT, VAT, AKCYZA, PCC) sprawdza API MF dla tego
     okna i dociaga do bazy WYLACZNIE nowe dokumenty (duplikaty pomijane
     automatycznie przez ON CONFLICT DO NOTHING w warstwie zapisu).
  3. Weryfikuje kompletnosc (drugie, niezalezne zapytanie do MF).
  4. Wysyla krotkie, codzienne powiadomienie mailowe z podsumowaniem.
  5. Zapisuje wpis w historii synchronizacji (widoczny w aplikacji).

Dlaczego okno 10 dni, nie 1 dzien: MF czasem publikuje interpretacje z data
wsteczna, i to z opoznieniem wiekszym niz kilka dni (np. interpretacja z
10.04 pojawia sie w API dopiero kilkanascie dni pozniej). Okno 3-dniowe,
uzywane wczesniej, dawalo obserwowalne ubytki w archiwum — dokumenty
istniejace w Eurece, ale publikowane "za pozno", zeby zlapac je w tak
waskim oknie. Przy oknie 10-dniowym kazdy dzien jest sprawdzany do
10-krotnie w kolejnych uruchomieniach, co daje znacznie wyzsza szanse
zlapania pozniej opublikowanych dokumentow, bez koniecznosci
przechowywania dodatkowego stanu miedzy uruchomieniami. Kosztem jest
wieksza liczba zapytan do API MF przy kazdym codziennym uruchomieniu.

Wymagane zmienne srodowiskowe:
  SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD
  GMAIL_ADRES, GMAIL_HASLO_APLIKACJI, EMAIL_ODBIORCA

Uruchomienie reczne (test):
  python synchronizacja_dzienna.py
"""

import os
import sys
import time

import db_core
import raport_silnik as silnik


MAKS_PROB_CALEGO_SYNC   = 3
ODSTEP_MIEDZY_PROBAMI_S = 600  # 10 minut


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
    statusy_wymagajace_retry = {"ERROR", "BLOKADA", "WERYFIKACJA_NIEUDANA"}
    return any(w["status"] in statusy_wymagajace_retry for w in wyniki)


def _wykonaj_probe(db, data_od, data_do, opis_okresu, numer_proby) -> list:
    wyniki = []
    for pod in silnik.PODATKI_WSZYSTKIE:
        print(f"\n--- {pod} (proba {numer_proby}) ---")
        wynik = silnik.generuj_raport_dla_podatku(
            db, pod, data_od, data_do, opis_okresu, log_fn=print, generuj_plik=False
        )
        wyniki.append(wynik)
        wer_info = ""
        if wynik.get("weryfikacja"):
            wer_info = f" | weryfikacja: {wynik['weryfikacja']['status']}"
        print(f"[{pod}] Status: {wynik['status']} | Dokumentow: {wynik['liczba_dok']} "
              f"| Nowych: {wynik['nowych_pobranych']}{wer_info}")
    return wyniki


def main():
    print("=" * 70)
    print("DorAIdca Radar — Codzienna Synchronizacja Interpretacji (3:00)")
    print("=" * 70)

    # Okno synchronizacji sterowane z workflow: częste przebiegi trzymają wąskie
    # okno (świeżość), a jeden nocny sięga szerzej (łapie publikacje opóźnione —
    # np. interpretacje wydane ponownie po wyroku, wpadające do Eureki z kilku-
    # tygodniowym poślizgiem). Brak zmiennej = zachowanie domyślne z raport_silnik.
    okno_env = os.environ.get("OKNO_SYNCHRONIZACJI_DNI")
    if okno_env:
        try:
            silnik.OKNO_SYNCHRONIZACJI_DNI = int(okno_env)
            print(f"Okno synchronizacji nadpisane z env: {okno_env} dni.")
        except ValueError:
            print(f"Nieprawidłowe OKNO_SYNCHRONIZACJI_DNI='{okno_env}' — używam domyślnego.")

    data_od, data_do, opis_okresu = silnik.zakres_synchronizacji()
    print(f"Okno: {data_od.date()} — {data_do.date()} ({opis_okresu})")

    config = _wczytaj_config_supabase()
    db = db_core.SupabaseDB(config)
    db.inicjalizuj_schemat()
    print("Polaczenie z Supabase OK.")

    wyniki = None
    proba = 1
    for proba in range(1, MAKS_PROB_CALEGO_SYNC + 1):
        wyniki = _wykonaj_probe(db, data_od, data_do, opis_okresu, proba)

        if not _czy_wymaga_ponowienia(wyniki):
            print(f"\nProba {proba}: wszystko OK, konczy petle retry.")
            break

        if proba < MAKS_PROB_CALEGO_SYNC:
            print(
                f"\nProba {proba}/{MAKS_PROB_CALEGO_SYNC} wykryla problemy z dostepnoscia MF. "
                f"Czekam {ODSTEP_MIEDZY_PROBAMI_S}s przed kolejna proba..."
            )
            time.sleep(ODSTEP_MIEDZY_PROBAMI_S)
        else:
            print(f"\nWyczerpano {MAKS_PROB_CALEGO_SYNC} prob. Wysylam powiadomienie z tym co udalo sie zebrac.")

    # ── POWIADOMIENIE MAILOWE — tylko na wyznaczonym przebiegu ──────────────
    # Przy kilku przebiegach dziennie mail-podsumowanie wysyłamy raz (nocny
    # przebieg ustawia SYNC_MAIL=1); pozostałe są ciche, żeby nie zasypać skrzynki.
    wyslij_mail = os.environ.get("SYNC_MAIL", "1") == "1"
    gmail_adres = os.environ.get("GMAIL_ADRES")
    gmail_haslo = os.environ.get("GMAIL_HASLO_APLIKACJI")
    odbiorca    = os.environ.get("EMAIL_ODBIORCA", gmail_adres)

    if not wyslij_mail:
        print("\nMail-podsumowanie wyciszony dla tego przebiegu (SYNC_MAIL != 1).")
    elif not gmail_adres or not gmail_haslo:
        print("\nBrak konfiguracji email — pomijam powiadomienie.")
    else:
        silnik.wyslij_email_synchronizacja_dzienna(
            wyniki, opis_okresu, gmail_adres, gmail_haslo, odbiorca, log_fn=print,
        )

    print("\n" + "=" * 70)
    print("PODSUMOWANIE KONCOWE:")
    for w in wyniki:
        wer = w.get("weryfikacja")
        wer_str = f" | weryfikacja: {wer['status']}" if wer else ""
        print(f"  {w['podatek']}: {w['liczba_dok']} dokumentow (nowych: {w['nowych_pobranych']}, "
              f"status: {w['status']}){wer_str}")
    print("=" * 70)

    # ── ZAPIS HISTORII (widoczne potem w Streamlit) ─────────────────────────
    try:
        statusy = [w["status"] for w in wyniki]
        if "ERROR" in statusy:
            status_ogolny = "ERROR"
        elif "NIEZGODNOSC" in statusy:
            status_ogolny = "NIEZGODNOSC"
        elif "WERYFIKACJA_NIEUDANA" in statusy:
            status_ogolny = "WERYFIKACJA_NIEUDANA"
        else:
            status_ogolny = "OK"

        for w in wyniki:
            wer = w.get("weryfikacja")
            szczegoly = ""
            if wer and wer["status"] == "NIEZGODNOSC":
                szczegoly = f"MF={wer['liczba_w_mf']}, archiwum={wer['liczba_w_archiwum']}"
            db_core.zapisz_historie_synchronizacji(
                db,
                data_od=data_od.strftime("%Y-%m-%d"),
                data_do=data_do.strftime("%Y-%m-%d"),
                podatek=w["podatek"],
                liczba_dok=w["liczba_dok"],
                nowych_dok=w["nowych_pobranych"],
                liczba_prob=proba,
                status=w["status"] if w["status"] != "BRAK_DOKUMENTOW" else "OK",
                szczegoly=szczegoly,
            )
        print(f"Zapisano historie synchronizacji dla {len(wyniki)} podatkow (status ogolny: {status_ogolny}).")
    except Exception as e:
        print(f"OSTRZEZENIE: nie udalo sie zapisac historii: {e}")

    bledy_krytyczne = [w for w in wyniki if w["status"] == "ERROR"]
    if bledy_krytyczne:
        sys.exit(1)


if __name__ == "__main__":
    main()
