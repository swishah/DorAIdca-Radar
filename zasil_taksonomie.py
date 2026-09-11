# -*- coding: utf-8 -*-
"""
zasil_taksonomie.py — przenosi taksonomię z kodu do bazy.

DLACZEGO CZYTA Z MODUŁU, A NIE Z WKLEJONEJ LISTY
  Wartości pobieramy importem ze streszczacz_openrouter.py, a nie przepisując
  je do skryptu. Przepisanie stu pozycji ręcznie to gwarantowana literówka,
  a literówka w taksonomii jest cicha: model wybierze wartość, walidacja ją
  odrzuci, streszczenie zapisze się bez branży i nigdy nie trafi w monitoring.

IDEMPOTENTNY
  Można uruchamiać wielokrotnie. Wartości istniejące są aktualizowane
  (kolejność), nowe dodawane, a te, których już nie ma w kodzie — OZNACZANE
  JAKO NIEAKTYWNE, nigdy kasowane. Kasowanie zerwałoby powiązanie z istniejącymi
  streszczeniami w streszczenia_auto.branze i z subskrypcjami w
  obserwowane_branze.

URUCHOMIENIE
  export SUPABASE_DB_URL="postgresql://..."
  python3 zasil_taksonomie.py            # pokazuje, co zrobi, i pyta
  python3 zasil_taksonomie.py --wykonaj  # zapisuje
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import db_core


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


PLIK_ZRODLOWY = "streszczacz_openrouter.py"


def _stale_przez_ast(sciezka: str) -> dict:
    """
    Wyciąga stałe BRANZE i PRZEDMIOTY parsując plik, BEZ jego importowania.

    DLACZEGO NIE IMPORT
      streszczacz_openrouter importuje requests i inne biblioteki potrzebne mu
      do pracy, ale zupełnie zbędne do odczytania dwóch list. Import wywracał
      się na brakującej zależności, a komunikat sugerował, że nie ma pliku —
      czyli wskazywał zupełnie nie tam, gdzie trzeba.

      Parsowanie drzewa składni czyta tylko to, co potrzebne, nie wykonuje
      żadnego kodu i nie obchodzi go, czego moduł potrzebuje do działania.
    """
    import ast

    if not os.path.exists(sciezka):
        raise SystemExit(
            f"Nie znaleziono pliku {sciezka}. Uruchom skrypt z katalogu "
            "repozytorium, obok streszczacza."
        )

    with open(sciezka, encoding="utf-8") as f:
        drzewo = ast.parse(f.read(), filename=sciezka)

    znalezione = {}
    for wezel in drzewo.body:                      # tylko poziom modułu
        if not isinstance(wezel, ast.Assign):
            continue
        for cel in wezel.targets:
            if isinstance(cel, ast.Name) and cel.id in ("BRANZE", "PRZEDMIOTY"):
                try:
                    znalezione[cel.id] = ast.literal_eval(wezel.value)
                except ValueError:
                    # Stała zbudowana wyrażeniem, nie literałem — wtedy nie ma
                    # innego wyjścia niż import.
                    pass
    return znalezione


def _wczytaj_z_kodu() -> tuple[list[str], dict[str, list[str]]]:
    """
    Pobiera taksonomię ze streszczacza. Najpierw parsowanie (bezpieczne,
    bez zależności), w ostateczności import.
    """
    stale = _stale_przez_ast(PLIK_ZRODLOWY)
    zrodlo = "parsowanie pliku"

    if "BRANZE" not in stale or "PRZEDMIOTY" not in stale:
        try:
            import streszczacz_openrouter as sopen
            stale.setdefault("BRANZE", getattr(sopen, "BRANZE", []))
            stale.setdefault("PRZEDMIOTY", getattr(sopen, "PRZEDMIOTY", {}))
            zrodlo = "import modułu"
        except ImportError as e:
            raise SystemExit(
                f"Nie udało się odczytać stałych z {PLIK_ZRODLOWY}.\n"
                f"Parsowanie nie znalazło ich jako zwykłych literałów, "
                f"a import zawiódł na zależności: {e}\n"
                "Dołóż brakującą bibliotekę do kroku instalacji w workflow."
            )

    branze = list(stale.get("BRANZE") or [])
    przedmioty = dict(stale.get("PRZEDMIOTY") or {})

    if not branze:
        raise SystemExit("BRANZE są puste — nie ma czego przenosić.")
    if not przedmioty:
        raise SystemExit("PRZEDMIOTY są puste — nie ma czego przenosić.")

    print(f"Źródło taksonomii: {PLIK_ZRODLOWY} ({zrodlo})")
    return branze, przedmioty


def _stan_bazy(db) -> tuple[dict[str, bool], dict[tuple[str, str], bool]]:
    """Zwraca {branza: aktywna} oraz {(podatek, przedmiot): aktywny}."""
    b, p = {}, {}
    try:
        for r in db.wykonaj("SELECT branza, aktywna FROM taksonomia_branze", fetch=True):
            b[r["branza"]] = r["aktywna"]
        for r in db.wykonaj(
                "SELECT podatek, przedmiot, aktywny FROM taksonomia_przedmiotow",
                fetch=True):
            p[(r["podatek"], r["przedmiot"])] = r["aktywny"]
    except Exception as e:
        raise SystemExit(
            "Nie udało się odczytać tabel taksonomii. Czy migracja "
            f"migracja_taksonomia.sql została wykonana? ({str(e).splitlines()[0]})"
        )
    return b, p


def main() -> int:
    ap = argparse.ArgumentParser(description="Przenosi taksonomię z kodu do bazy.")
    ap.add_argument("--wykonaj", action="store_true",
                    help="faktycznie zapisz (bez tego tylko pokazuje plan)")
    args = ap.parse_args()

    branze, przedmioty = _wczytaj_z_kodu()
    db = _polacz()
    w_bazie_b, w_bazie_p = _stan_bazy(db)

    teraz = dt.datetime.now().isoformat(timespec="seconds")

    print("=" * 74)
    print("ZASILENIE TAKSONOMII" + ("" if args.wykonaj else "  —  PODGLĄD, nic nie zapisuję"))
    print("=" * 74)

    # ── BRANŻE ──
    nowe_b = [b for b in branze if b not in w_bazie_b]
    do_wycofania_b = [b for b, akt in w_bazie_b.items() if akt and b not in branze]

    print(f"\nBRANŻE  (w kodzie: {len(branze)}, w bazie: {len(w_bazie_b)})")
    print(f"  do dodania:   {len(nowe_b)}")
    for b in nowe_b[:20]:
        print(f"     + {b}")
    if len(nowe_b) > 20:
        print(f"     ... i {len(nowe_b) - 20} dalszych")
    print(f"  do wycofania: {len(do_wycofania_b)}")
    for b in do_wycofania_b:
        print(f"     - {b}   (zostaje w bazie, oznaczona jako nieaktywna)")

    # ── PRZEDMIOTY ──
    pary = [(pod, prz) for pod, lista in przedmioty.items() for prz in lista]
    nowe_p = [x for x in pary if x not in w_bazie_p]
    do_wycofania_p = [k for k, akt in w_bazie_p.items() if akt and k not in set(pary)]

    print(f"\nPRZEDMIOTY  (w kodzie: {len(pary)}, w bazie: {len(w_bazie_p)})")
    for pod, lista in przedmioty.items():
        print(f"     {pod}: {len(lista)}")
    print(f"  do dodania:   {len(nowe_p)}")
    print(f"  do wycofania: {len(do_wycofania_p)}")
    for pod, prz in do_wycofania_p:
        print(f"     - [{pod}] {prz}")

    if not args.wykonaj:
        print("\n" + "-" * 74)
        print("To był podgląd. Żeby zapisać, uruchom ponownie z --wykonaj")
        return 0

    # ── ZAPIS ──
    print("\n" + "-" * 74)
    print("ZAPISUJĘ")

    dane_b = [(b, i, True, teraz) for i, b in enumerate(branze)]
    db.wykonaj_wiele(
        """INSERT INTO taksonomia_branze (branza, kolejnosc, aktywna, dodano)
           VALUES %s
           ON CONFLICT (branza) DO UPDATE SET
               kolejnosc = EXCLUDED.kolejnosc,
               aktywna   = TRUE""",
        dane_b)
    print(f"  branże: zapisano {len(dane_b)}")

    dane_p = [(pod, prz, i, True, teraz)
              for pod, lista in przedmioty.items()
              for i, prz in enumerate(lista)]
    db.wykonaj_wiele(
        """INSERT INTO taksonomia_przedmiotow
               (podatek, przedmiot, kolejnosc, aktywny, dodano)
           VALUES %s
           ON CONFLICT (podatek, przedmiot) DO UPDATE SET
               kolejnosc = EXCLUDED.kolejnosc,
               aktywny   = TRUE""",
        dane_p)
    print(f"  przedmioty: zapisano {len(dane_p)}")

    # Wycofanie — oznaczenie, nigdy DELETE.
    if do_wycofania_b:
        db.wykonaj("UPDATE taksonomia_branze SET aktywna = FALSE WHERE branza = ANY(%s)",
                   ([b for b in do_wycofania_b],))
        print(f"  branże wycofane: {len(do_wycofania_b)}")
    if do_wycofania_p:
        for pod, prz in do_wycofania_p:
            db.wykonaj(
                "UPDATE taksonomia_przedmiotow SET aktywny = FALSE "
                "WHERE podatek = %s AND przedmiot = %s", (pod, prz))
        print(f"  przedmioty wycofane: {len(do_wycofania_p)}")

    # ── KONTROLA ──
    kontrola = db.wykonaj(
        """SELECT (SELECT count(*) FROM taksonomia_branze WHERE aktywna) AS b,
                  (SELECT count(*) FROM taksonomia_przedmiotow WHERE aktywny) AS p""",
        fetch=True)[0]
    print(f"\n  W bazie aktywnych: {kontrola['b']} branż, {kontrola['p']} przedmiotów")

    zgadza_sie = kontrola["b"] == len(branze) and kontrola["p"] == len(pary)
    print("  " + ("Zgadza się z kodem." if zgadza_sie
                  else "UWAGA: liczby nie zgadzają się z kodem — sprawdź powyższe."))
    return 0 if zgadza_sie else 1


if __name__ == "__main__":
    sys.exit(main())
