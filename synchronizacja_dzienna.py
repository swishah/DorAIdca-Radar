#!/usr/bin/env python3
"""
synchronizacja_dzienna.py — Codzienna automatyczna synchronizacja bazy
interpretacji indywidualnych. Uruchamiany przez GitHub Actions codziennie
o 3:00 w nocy.

Co robi:
  1. Wyznacza ruchome okno: 5 dni w przebiegach rutynowych, 30 dni w niedzielnym
     (workflow ustawia OKNO_SYNCHRONIZACJI_DNI; patrz raport_silnik.py).
  2. Dla kazdego podatku (PIT, CIT, VAT, AKCYZA, PCC) sprawdza API MF dla tego
     okna i dociaga do bazy WYLACZNIE nowe dokumenty (duplikaty pomijane
     automatycznie przez ON CONFLICT DO NOTHING w warstwie zapisu).
  3. Weryfikuje kompletnosc (drugie, niezalezne zapytanie do MF).
  4. Wysyla krotkie, codzienne powiadomienie mailowe z podsumowaniem.
  5. Zapisuje wpis w historii synchronizacji (widoczny w aplikacji).

Dlaczego okno ruchome, a nie jeden dzien: MF czasem publikuje interpretacje
z data wsteczna, i to z opoznieniem wiekszym niz kilka dni (np. interpretacja
z 10.04 pojawia sie w API dopiero kilkanascie dni pozniej). Okno 3-dniowe,
uzywane wczesniej, dawalo obserwowalne ubytki w archiwum.

Dlaczego 5 dni na co dzien, a 30 raz w tygodniu (wrzesien 2026): przy dwoch
przebiegach dziennie kazdy dzien wpada w kontrole dziesieciokrotnie, a to,
co MF opublikuje z kilkutygodniowym poslizgiem, lapie niedzielny przebieg.
Wczesniejsze okno 10-dniowe przy kazdym uruchomieniu bylo najwiekszym
stalym obciazeniem, jakie robimy API MF — a blokada adresu, ktora spotkala
biuro 10 wrzesnia, kosztuje wiecej niz kilka godzin dodatkowego opoznienia
w wylapaniu spoznionej publikacji. Duplikaty i tak sa pomijane przy zapisie
(ON CONFLICT DO NOTHING), wiec powtorne sprawdzanie tych samych dni jest
bezpieczne.

TRYBY (zmienna TRYB_SYNC — wejscie "tryb" przy recznym uruchomieniu workflow)
  zwykly   wbudowana piatka i podatki dodane z EUREKI, ruchome okno. Tak ida
           wszystkie przebiegi z harmonogramu.
  podatek  JEDEN podatek dodany z EUREKI (PODATEK_SYNC, np. CUKIER) od jego daty
           startu — dnia dodania minus tydzien. Pierwsze pobranie, ktore
           wykonawca w Dockerze zleca zaraz po dodaniu podatku.
  slownik  bez interpretacji: lista ustaw ze slownika przepisow EUREKI do tabeli
           eureka_przepisy. Z niej administrator wybiera nowy podatek.
  sprawdz  diagnostyka, NIC nie zapisuje: ile interpretacji ma w EUREKA ustawa
           dodanego podatku (PODATEK_SYNC) w ostatnim roku i ile z nich baza ma
           juz pod innym podatkiem (MF przypisuje interpretacje do kilku ustaw).

Wymagane zmienne srodowiskowe:
  SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD
  GMAIL_ADRES, GMAIL_HASLO_APLIKACJI, EMAIL_ODBIORCA

Uruchomienie reczne (test):
  python synchronizacja_dzienna.py
  TRYB_SYNC=podatek PODATEK_SYNC=CUKIER python synchronizacja_dzienna.py
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests

import db_core
import raport_silnik as silnik
import utils


MAKS_PROB_CALEGO_SYNC   = 3
ODSTEP_MIEDZY_PROBAMI_S = 600  # 10 minut

TRYBY = ("zwykly", "podatek", "slownik", "sprawdz")
SPRAWDZ_DNI = 365
# Pierwsze pobranie nowego podatku siega do jego daty startu, ale nie dalej
# niz tyle dni wstecz — ponowne uruchomienie po miesiacach nie zamieni sie
# w wielomiesieczne pobieranie (od tego jest codzienne okno).
MAKS_OKNO_PODATKU_DNI = 31


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


def _wykonaj_probe(db, okna, opis_okresu, numer_proby) -> list:
    """okna: {podatek: (data_od, data_do)} — podatek dodany z EUREKI ma okno
    przyciete do swojej daty startu."""
    wyniki = []
    for pod, (data_od, data_do) in okna.items():
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


def _pobierz_slownik(db) -> None:
    """Tryb "slownik": lista ustaw z EUREKI do tabeli eureka_przepisy."""
    with requests.Session() as sesja:
        pozycje = utils.pobierz_slownik_przepisow(sesja, log_fn=print)
    zmienione = db_core.zapisz_slownik_przepisow(db, pozycje)
    print(f"Slownik przepisow w chmurze: {len(pozycje)} pozycji "
          f"(nowych albo zmienionych: {zmienione}).")


def _sprawdz(db, dodane: list) -> None:
    """Tryb "sprawdz": sama lista z MF (sygnatury i daty, bez tresci) i jej
    porownanie z baza. Nic nie zapisuje."""
    kod = (os.environ.get("PODATEK_SYNC") or "").strip().upper()
    if kod not in dodane:
        raise SystemExit(f"Podatek '{kod}' nie jest aktywnym podatkiem dodanym z EUREKI.")
    data_do = datetime.now()
    data_od = data_do - timedelta(days=SPRAWDZ_DNI)
    with requests.Session() as sesja:
        lista, status = utils.pobierz_wszystko_z_okresu(
            data_od.strftime("%Y-%m-%d"), data_do.strftime("%Y-%m-%d"), sesja, kod,
            utils.KODY_PRZEPISOW[kod], log_fn=print)
    ids = [d["id"] for d in lista]
    w_bazie = {r["id"]: r["podatek"] for r in db.wykonaj(
        "SELECT id, podatek FROM dokumenty WHERE id = ANY(%s)", (ids,), fetch=True)} if ids else {}
    print(f"\n[{kod}] EUREKA, {data_od.date()} — {data_do.date()}: {len(lista)} interpretacji "
          f"(status listy: {status})")
    pod_innym = {}
    for pid, pod in w_bazie.items():
        pod_innym[pod] = pod_innym.get(pod, 0) + 1
    print(f"[{kod}] juz w bazie: {len(w_bazie)} — "
          + (", ".join(f"{p}: {n}" for p, n in sorted(pod_innym.items())) or "zadnej"))
    print(f"[{kod}] brak w bazie: {len(ids) - len(w_bazie)}")
    miesiace = {}
    for d in lista:
        miesiace[d["data"][:7]] = miesiace.get(d["data"][:7], 0) + 1
    print(f"[{kod}] wg miesiecy: " + ", ".join(f"{m}: {n}" for m, n in sorted(miesiace.items())))
    for d in sorted(lista, key=lambda x: x["data"], reverse=True)[:25]:
        print(f"    {d['data']}  {d['sygnatura']:<40} {w_bazie.get(d['id'], '— brak w bazie')}")


def _okna(tryb: str, dodane: list) -> tuple:
    """({podatek: (data_od, data_do)}, opis_okresu) dla trybu zwykly albo podatek."""
    if tryb == "podatek":
        kod = (os.environ.get("PODATEK_SYNC") or "").strip().upper()
        if kod not in dodane:
            raise SystemExit(f"Podatek '{kod}' nie jest aktywnym podatkiem dodanym z EUREKI "
                             f"(aktywne: {', '.join(dodane) or 'brak'}) — nic do zrobienia.")
        data_do = datetime.now()
        start = datetime.strptime(utils.data_start(kod), "%Y-%m-%d")
        data_od = max(start, data_do - timedelta(days=MAKS_OKNO_PODATKU_DNI - 1))
        opis = f"{data_od.strftime('%d.%m')} — {data_do.strftime('%d.%m.%Y')}"
        return {kod: (data_od, data_do)}, opis

    data_od, data_do, opis = silnik.zakres_synchronizacji()
    okna = {pod: (data_od, data_do) for pod in silnik.PODATKI_WSZYSTKIE}
    for pod in dodane:
        okno = silnik.okno_podatku(pod, data_od, data_do)
        if okno:
            okna[pod] = okno
        else:
            print(f"[{pod}] pobieranie rusza od {utils.data_start(pod)} — pomijam.")
    return okna, opis


def main():
    tryb = (os.environ.get("TRYB_SYNC") or "zwykly").strip().lower()
    if tryb not in TRYBY:
        raise SystemExit(f"Nieznany tryb '{tryb}' — dozwolone: {', '.join(TRYBY)}.")

    print("=" * 70)
    print("DorAIdca Radar — Codzienna Synchronizacja Interpretacji"
          + ("" if tryb == "zwykly" else f"  [tryb: {tryb}]"))
    print("=" * 70)

    # Okno synchronizacji sterowane z workflow: rutynowe przebiegi trzymają
    # wąskie okno (5 dni), a niedzielny sięga szerzej (30 dni — łapie
    # publikacje opóźnione, np. interpretacje wydane ponownie po wyroku,
    # wpadające do Eureki z kilkutygodniowym poślizgiem). OKNO_RECZNE ma
    # pierwszeństwo: to wejście „okno" przy ręcznym uruchomieniu.
    # Brak obu = zachowanie domyślne z raport_silnik.
    okno_env = os.environ.get("OKNO_RECZNE") or os.environ.get("OKNO_SYNCHRONIZACJI_DNI")
    if okno_env:
        try:
            silnik.OKNO_SYNCHRONIZACJI_DNI = int(okno_env)
            print(f"Okno synchronizacji nadpisane z env: {okno_env} dni.")
        except ValueError:
            print(f"Nieprawidłowe OKNO_SYNCHRONIZACJI_DNI='{okno_env}' — używam domyślnego.")

    config = _wczytaj_config_supabase()
    db = db_core.SupabaseDB(config)
    db.inicjalizuj_schemat()
    print("Polaczenie z Supabase OK.")

    if tryb == "slownik":
        _pobierz_slownik(db)
        return

    dodane = silnik.dolacz_podatki_eureka(db)
    if dodane:
        print("Podatki dodane z EUREKI: " + ", ".join(
            f"{k} (ustawa nr {utils.KODY_PRZEPISOW[k]}, od {utils.data_start(k)})" for k in dodane))
    if tryb == "sprawdz":
        _sprawdz(db, dodane)
        return
    okna, opis_okresu = _okna(tryb, dodane)
    for pod, (od, do) in okna.items():
        print(f"Okno {pod}: {od.date()} — {do.date()}")

    wyniki = None
    proba = 1
    for proba in range(1, MAKS_PROB_CALEGO_SYNC + 1):
        wyniki = _wykonaj_probe(db, okna, opis_okresu, proba)

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
    # Pierwsze pobranie nowego podatku nie wysyla dziennego podsumowania —
    # to nie jest dzienny przebieg, a jego wynik widac w module Harmonogram.
    wyslij_mail = tryb == "zwykly" and os.environ.get("SYNC_MAIL", "1") == "1"
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
            data_od, data_do = okna[w["podatek"]]
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
