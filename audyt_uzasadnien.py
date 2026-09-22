#!/usr/bin/env python3
"""
audyt_uzasadnien.py — sprawdza, czy modul wyrokow NAPRAWDE wylapuje
uzasadnienia pojawiajace sie na CBOSA juz po publikacji sentencji.

PO CO TO JEST
  Wyrok trafia do bazy najpierw jako sama sentencja (status
  OCZEKUJE_NA_UZASADNIENIE). Uzasadnienie dochodzi po tygodniach albo
  miesiacach i ma je dociagnac strumien 2 cotygodniowej synchronizacji.
  Gdy ten strumien stanie, nie widac tego po niczym: rekordy sa w bazie,
  liczniki rosna, tylko uzasadnien nie przybywa. Dokladnie tak bylo miedzy
  19.07.2026 a 22.09.2026 — strumien 2 nie chodzil ani razu, a z 526 wyrokow
  od sierpnia 2026 zadne nie dostalo uzasadnienia.

JAK MIERZY (dwa niezalezne sprawdzenia, celowo)
  1. LISTA — pytamy CBOSA filtrem "z uzasadnieniem" o okno wstecz
     i porownujemy odpowiedz z baza:
       MAMY         status KOMPLETNY,
       LUKA_TRESCI  rekord jest, ale bez uzasadnienia,
       LUKA_REKORDU nie ma go u nas wcale.
     Kazda luka wieksza od zera znaczy, ze funkcja nie spelnia swojej roli.

     UWAGA CO DO OKNA: CBOSA opisuje te pola jako "Data orzeczenia", ale
     w odpowiedzi potrafi dolozyc starsze wyroki (widziane: 2025-04-29
     w oknie lipiec–wrzesien 2026) — najpewniej te, ktorych uzasadnienie
     opublikowano w oknie. Dla audytu to zysk, nie klopot: interesuje nas
     wlasnie to, co CBOSA udostepnil niedawno. Wynik czytamy wiec jako
     "co jest teraz dostepne z uzasadnieniem", nie "co orzeczono w oknie".
  2. PROBKA — bierzemy N najnowszych rekordow OCZEKUJACYCH i otwieramy ich
     strony /doc/<ID>, sprawdzajac, czy uzasadnienie juz tam jest. To
     sprawdzenie nie ufa filtrowi CBOSA: gdyby filtr przestal dzialac albo
     zmienil znaczenie, samo porownanie list pokazywaloby zero luk i audyt
     bylby slepy dokladnie tam, gdzie ma patrzec.

GDZIE URUCHAMIAC
  Z Dockera albo z sieci biura. CBOSA od 11.09.2026 zrywa polaczenie TLS
  z adresami GitHub Actions (SSLEOFError na /cbo/query), wiec audyt
  uruchomiony w Actions zmierzy tylko wlasny brak dostepu — i tak wlasnie
  to zglosi, zamiast udawac, ze jest czysto.

Kod wyjscia: 0 gdy czysto, 1 gdy audyt znalazl luke albo nie mogl jej
zmierzyc — zeby dalo sie go powiesic na alarmie.
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

import db_core
import db_wyroki
import utils
import wyroki_cbosa as cbosa


DOMYSLNE_OKNO_DNI = 180      # tyle samo, ile okno strumienia uzasadnien
DOMYSLNA_PROBKA   = 10       # ile stron /doc/<ID> otwieramy na drugie sprawdzenie


def _config_supabase() -> dict:
    return {
        "host":     os.environ["SUPABASE_HOST"],
        "port":     os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user":     os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode":  os.environ.get("SUPABASE_SSLMODE", "require"),
    }


# ---------------------------------------------------------------------------
# CZESC 1 — stan samej maszynerii (bez pytania CBOSA)
# ---------------------------------------------------------------------------
def stan_maszynerii(db, log=print) -> int:
    """Kiedy ostatnio chodzil ktory strumien i czy tryb lekki nie wycial str. 2."""
    log("")
    log("=" * 70)
    log("1. STAN MASZYNERII")
    log("=" * 70)
    uwagi = 0

    tryb_lekki = (os.environ.get("WYROKI_TYLKO_METADANE", "1") == "1")
    log("   WYROKI_TYLKO_METADANE = %s" % ("1 (TRYB LEKKI)" if tryb_lekki else "0"))
    if tryb_lekki:
        log("   UWAGA: w tym trybie synchronizacja POMIJA strumien uzasadnien.")
        log("          Uzasadnienia nie beda dochodzic, dopoki nie ustawisz 0.")
        uwagi += 1

    wiersze = db.wykonaj(
        """SELECT strumien, max(uruchomiono) AS ostatni, count(*) AS przebiegow
           FROM historia_sync_wyrokow GROUP BY strumien ORDER BY strumien""",
        fetch=True) or []
    dzis = datetime.now()
    znane = {w["strumien"]: w for w in wiersze}
    # Progi: metadane chodza co tydzien (10 dni zapasu), pozostale strumienie
    # tez co tydzien, ale daja sie przespac jedna niedziele bez szkody.
    for strumien, prog_dni in (("METADANE", 10), ("UZASADNIENIA", 14),
                               ("PRAWOMOCNOSC", 14)):
        w = znane.get(strumien)
        if not w:
            log("   %-13s NIGDY nie chodzil." % strumien)
            uwagi += 1
            continue
        try:
            wiek = (dzis - datetime.fromisoformat(w["ostatni"])).days
        except (TypeError, ValueError):
            wiek = None
        znacznik = ""
        if wiek is not None and wiek > prog_dni:
            znacznik = "   <-- STOI (prog %d dni)" % prog_dni
            uwagi += 1
        log("   %-13s %s (%s dni temu)%s"
            % (strumien, w["ostatni"], wiek if wiek is not None else "?", znacznik))

    w = db.wykonaj(
        """SELECT count(*) AS n, max(data_orzeczenia) AS najnowszy
           FROM wyroki WHERE length(uzasadnienie) > 0""", fetch=True)[0]
    log("   Uzasadnien w bazie: %d, najnowsze orzeczenie z uzasadnieniem: %s"
        % (w["n"], w["najnowszy"] or "-"))

    w = db.wykonaj(
        "SELECT count(*) AS n FROM wyroki WHERE status_tresci = %s",
        (db_wyroki.STATUS_OCZEKUJE,), fetch=True)[0]
    log("   Rekordow OCZEKUJE_NA_UZASADNIENIE: %d" % w["n"])
    return uwagi


# ---------------------------------------------------------------------------
# CZESC 2 — porownanie list: co CBOSA ma z uzasadnieniem, a czego my nie mamy
# ---------------------------------------------------------------------------
def porownaj_listy(db, sesja, formularz, dni: int, log=print) -> dict:
    do = datetime.now().strftime("%Y-%m-%d")
    od = (datetime.now() - timedelta(days=dni)).strftime("%Y-%m-%d")
    log("")
    log("=" * 70)
    log("2. LISTA CBOSA (filtr: z uzasadnieniem) vs BAZA | okno %s..%s" % (od, do))
    log("=" * 70)

    znane = db_wyroki.pobierz_id_wyrokow(db)
    suma = {"cbosa": 0, "mamy": 0, "luka_tresci": 0, "luka_rekordu": 0,
            "bledy": 0, "braki": []}

    for podatek, symbol in cbosa.SYMBOLE_PODATKOW.items():
        prog = utils.data_start(podatek)
        if do < prog:
            log("   [%s] pominiety — start podatku %s" % (podatek, prog))
            continue
        od_p = max(od, prog)
        try:
            lista, _total = cbosa.szukaj(sesja, formularz, symbol, od_p, do,
                                         tylko_z_uzasadnieniem=True, log_fn=None)
        except cbosa.BladCBOSA as e:
            log("   [%s] BLAD zapytania: %s" % (podatek, e))
            suma["bledy"] += 1
            continue

        mamy = luka_t = luka_r = 0
        for did, tytul in lista:
            status = znane.get(did)
            if status == db_wyroki.STATUS_KOMPLETNY:
                mamy += 1
            elif status is None:
                luka_r += 1
                suma["braki"].append((podatek, did, "BRAK REKORDU", tytul[:70]))
            else:
                luka_t += 1
                suma["braki"].append((podatek, did, status, tytul[:70]))

        suma["cbosa"] += len(lista)
        suma["mamy"] += mamy
        suma["luka_tresci"] += luka_t
        suma["luka_rekordu"] += luka_r
        log("   [%-6s] CBOSA %4d | mamy %4d | bez tresci %4d | bez rekordu %4d"
            % (podatek, len(lista), mamy, luka_t, luka_r))

    log("   " + "-" * 64)
    log("   RAZEM     CBOSA %4d | mamy %4d | bez tresci %4d | bez rekordu %4d"
        % (suma["cbosa"], suma["mamy"], suma["luka_tresci"], suma["luka_rekordu"]))
    if suma["cbosa"]:
        log("   Pokrycie: %.1f%%" % (100.0 * suma["mamy"] / suma["cbosa"]))
    return suma


# ---------------------------------------------------------------------------
# CZESC 3 — probka stron /doc/<ID>: sprawdzenie, ktore nie ufa filtrowi CBOSA
# ---------------------------------------------------------------------------
def probka_oczekujacych(db, sesja, ile: int, log=print) -> dict:
    log("")
    log("=" * 70)
    log("3. PROBKA %d NAJNOWSZYCH OCZEKUJACYCH — czy uzasadnienie juz jest" % ile)
    log("=" * 70)
    wiersze = db.wykonaj(
        """SELECT id, sygnatura, data_orzeczenia, podatek FROM wyroki
           WHERE status_tresci = %s AND data_orzeczenia <> ''
           ORDER BY data_orzeczenia DESC LIMIT %s""",
        (db_wyroki.STATUS_OCZEKUJE, ile), fetch=True) or []

    wynik = {"sprawdzone": 0, "z_uzasadnieniem": 0, "bledy": 0, "przyklady": []}
    for w in wiersze:
        try:
            szcz = cbosa.pobierz_szczegoly(sesja, w["id"], log_fn=None)
        except Exception as e:
            wynik["bledy"] += 1
            log("   %-16s %s  BLAD: %s" % (w["sygnatura"], w["data_orzeczenia"], e))
            continue
        dlugosc = len(szcz.get("uzasadnienie") or "")
        wynik["sprawdzone"] += 1
        if dlugosc > 0:
            wynik["z_uzasadnieniem"] += 1
            wynik["przyklady"].append((w["sygnatura"], w["id"], dlugosc))
        log("   %-16s %s  %-6s uzasadnienie: %s"
            % (w["sygnatura"], w["data_orzeczenia"], w["podatek"] or "-",
               ("%d znakow  <-- PRZEGAPIONE" % dlugosc) if dlugosc else "jeszcze brak"))
    return wynik


def main():
    p = argparse.ArgumentParser(description="Audyt wylapywania uzasadnien wyrokow")
    p.add_argument("--dni", type=int, default=DOMYSLNE_OKNO_DNI,
                   help="okno wstecz dla porownania list (dom. %d)" % DOMYSLNE_OKNO_DNI)
    p.add_argument("--probka", type=int, default=DOMYSLNA_PROBKA,
                   help="ile stron /doc/<ID> otworzyc (0 = pomin, dom. %d)" % DOMYSLNA_PROBKA)
    p.add_argument("--bez-listy", action="store_true",
                   help="pomin porownanie list (szybki audyt na samej probce)")
    p.add_argument("--plik", default="",
                   help="zapisz WSZYSTKIE braki do pliku CSV (na ekran idzie 20)")
    args = p.parse_args()

    print("=" * 70)
    print("DorAIdca Radar — AUDYT WYLAPYWANIA UZASADNIEN")
    print("Uruchomiono: %s" % datetime.now().isoformat(timespec="seconds"))
    print("=" * 70)

    db = db_core.SupabaseDB(_config_supabase())
    uwagi = stan_maszynerii(db)

    luki, przegapione, probka_n = 0, 0, 0
    if not args.bez_listy or args.probka > 0:
        try:
            sesja = cbosa.nowa_sesja()
            formularz = cbosa.poznaj_formularz(sesja, log_fn=None)
        except Exception as e:
            print("")
            print("NIE MOZNA SIEGNAC DO CBOSA: %s" % e)
            print("Audyt uruchomiono z adresu, ktoremu CBOSA nie odpowiada — tak")
            print("wlasnie wyglada blokada GitHub Actions trwajaca od 11.09.2026.")
            print("Uruchom go z Dockera albo z sieci biura.")
            sys.exit(1)

        if not args.bez_listy:
            suma = porownaj_listy(db, sesja, formularz, args.dni)
            luki = suma["luka_tresci"] + suma["luka_rekordu"]
            if suma["braki"]:
                print("")
                print("   Pierwsze 20 brakow:")
                for podatek, did, status, tytul in suma["braki"][:20]:
                    print("     %-6s %-12s %-24s %s" % (podatek, did, status, tytul))
                    print("            https://orzeczenia.nsa.gov.pl/doc/%s" % did)
                if args.plik:
                    # Cala lista do pliku — na ekranie zostaje 20 pozycji, bo
                    # przy kilkuset brakach reszta i tak zniknelaby w logu.
                    with open(args.plik, "w", encoding="utf-8") as f:
                        f.write("podatek;id;status;tytul;link\n")
                        for podatek, did, status, tytul in suma["braki"]:
                            f.write("%s;%s;%s;%s;https://orzeczenia.nsa.gov.pl/doc/%s\n"
                                    % (podatek, did, status,
                                       tytul.replace(";", ","), did))
                    print("")
                    print("   Pelna lista (%d pozycji): %s"
                          % (len(suma["braki"]), args.plik))

        if args.probka > 0:
            pr = probka_oczekujacych(db, sesja, args.probka)
            przegapione, probka_n = pr["z_uzasadnieniem"], pr["sprawdzone"]

    print("")
    print("=" * 70)
    print("WYNIK AUDYTU")
    print("=" * 70)
    if luki == 0 and przegapione == 0 and uwagi == 0:
        print("CZYSTO — uzasadnienia sa wylapywane na biezaco.")
        sys.exit(0)
    if luki:
        print("LUKA: %d orzeczen ma na CBOSA uzasadnienie, ktorego nie mamy." % luki)
    if przegapione:
        print("PROBKA: %d z %d sprawdzonych rekordow OCZEKUJACYCH ma juz "
              "uzasadnienie na CBOSA." % (przegapione, probka_n))
    if uwagi:
        print("UWAGI DO MASZYNERII: %d (patrz czesc 1)." % uwagi)
    print("")
    print("Naprawa: uruchom synchronizacje ze strumieniem 2 i wylaczonym trybem")
    print("lekkim, z adresu, ktoremu CBOSA odpowiada:")
    print("  WYROKI_TYLKO_METADANE=0 python synchronizacja_wyrokow.py --strumienie 2")
    sys.exit(1)


if __name__ == "__main__":
    main()
