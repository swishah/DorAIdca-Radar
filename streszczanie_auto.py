# -*- coding: utf-8 -*-
"""
streszczanie_auto.py — PEŁNY AUTOMAT streszczania (GitHub Actions).
Niezależny od Streamlit. Klucze czytane z os.environ.

Co robi przy każdym uruchomieniu:
  1. Znajduje w bazie interpretacje wydane od DATA_START (włącznie), które
     NIE mają jeszcze sensownego streszczenia dla wybranego modelu.
  2. Streszcza je przez OpenRouter (ten sam klient co moduł 6) i zapisuje
     do streszczenia_auto.
Skrypt jest IDEMPOTENTNY — streszcza tylko brakujące. Dlatego „ponawianie”
realizuje harmonogram cykliczny w workflow: jeśli przebieg utknie na limicie
(429) i przerwie, kolejne uruchomienie (np. godzinę później) dokończy resztę.

Wymagane zmienne środowiskowe (z GitHub Secrets):
  OPENROUTER_API_KEY   — klucz OpenRouter (wymagany)
  SUPABASE_DB_URL      — postgresql://user:haslo@host:port/postgres  (zalecane)
      LUB pojedynczo: SUPABASE_HOST, SUPABASE_USER, SUPABASE_PASSWORD,
                      SUPABASE_PORT (domyślnie 5432), SUPABASE_DB (domyślnie postgres)
Opcjonalne:
  OPENROUTER_MODEL     — domyślnie openrouter/free
  STRESZCZ_PRZERWA_S   — odstęp między zapytaniami (domyślnie 3.5 s, ~17/min)
  STRESZCZ_MAKS_NA_RUN — ile pozycji na jedno uruchomienie (domyślnie 40)
  STRESZCZ_DATA_START  — próg daty wydania (domyślnie 2026-07-15)
"""

from __future__ import annotations

import os
import sys
import time

import db_core
import streszczacz_openrouter as sopen
import utils

PODATKI = ["PIT", "CIT", "VAT", "AKCYZA", "PCC"]
# UWAGA: używamy `or`, nie os.environ.get(klucz, domyslna) — bo workflow
# ZAWSZE ustawia te zmienne, a przy pustym polu (harmonogram albo puste pole
# ręcznego uruchomienia) trafia tu PUSTY łańcuch. get(...) podstawiłby domyślną
# tylko przy braku zmiennej, nie przy pustym łańcuchu — stąd wcześniej pusty
# model i błąd „No models provided”.
# Najwczesniejszy prog sposrod wszystkich podatkow — uzywany w zapytaniu jako
# wstepne odsianie. Progi per podatek (utils.DATY_START_PODATKU) stosowane sa
# zaraz po pobraniu; patrz _do_streszczenia().
DATA_START = os.environ.get("STRESZCZ_DATA_START") or utils.DATA_START_DOMYSLNA
MODEL = os.environ.get("OPENROUTER_MODEL") or sopen.MODEL_DOMYSLNY
PRZERWA_S = float(os.environ.get("STRESZCZ_PRZERWA_S") or "3.5")
MAKS_NA_RUN = int(os.environ.get("STRESZCZ_MAKS_NA_RUN") or "40")


# ---------------------------------------------------------------------------
def _polacz() -> db_core.SupabaseDB:
    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return db_core.SupabaseDB({"url": url})
    braki = [k for k in ("SUPABASE_HOST", "SUPABASE_USER", "SUPABASE_PASSWORD")
             if not os.environ.get(k)]
    if braki:
        raise SystemExit(
            "Brak konfiguracji bazy. Ustaw SUPABASE_DB_URL albo "
            + ", ".join(braki) + "."
        )
    return db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode": os.environ.get("SUPABASE_SSLMODE", "require"),
    })


def _zapewnij_tabele(db: db_core.SupabaseDB) -> None:
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS streszczenia_auto (
            id           SERIAL PRIMARY KEY,
            dokument_id  TEXT NOT NULL,
            podatek      TEXT NOT NULL,
            model        TEXT NOT NULL,
            temat        TEXT DEFAULT '',
            streszczenie TEXT DEFAULT '',
            wygenerowano TEXT NOT NULL,
            UNIQUE (dokument_id, model)
        )
        """
    )
    db.wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS branze TEXT DEFAULT ''"
    )
    db.wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS przedmiot TEXT DEFAULT ''"
    )


def _sensowne(s: str | None) -> bool:
    return not sopen.streszczenie_wadliwe(s)


def _do_streszczenia(db: db_core.SupabaseDB, model: str) -> list[dict]:
    # WAŻNE dla egressu: NIE pobieramy tu d.tekst. Wcześniej zapytanie
    # ściągało pełne teksty WSZYSTKICH interpretacji od DATA_START przy każdym
    # przebiegu (także dawno streszczonych) — to był największy pojedynczy
    # wyciek egressu. Teraz czytamy tylko lekkie metadane i małe streszczenie
    # do wykrycia braków; pełne teksty dobieramy dopiero dla przetwarzanej
    # garstki (_tekst_dla), już po ograniczeniu do MAKS_NA_RUN.
    rows = db.wykonaj(
        """
        SELECT d.id, d.podatek, d.sygnatura, d.data_wyd,
               s.streszczenie AS s_streszcz
        FROM dokumenty d
        LEFT JOIN streszczenia_auto s
               ON s.dokument_id = d.id AND s.model = %s
        WHERE d.podatek = ANY(%s)
          AND d.data_wyd >= %s
        ORDER BY d.data_wyd, d.sygnatura
        """,
        (model, PODATKI, DATA_START),
        fetch=True,
    )
    # Prog daty jest w zapytaniu ustawiony na NAJWCZESNIEJSZY z progow
    # (DATA_START = 15.07). Podatki o pozniejszym starcie — PCC od 03.08 —
    # odsiewamy tutaj. Robimy to po stronie Pythona, a nie w SQL, bo wiersze
    # sa juz pobrane i lekkie (same metadane, bez tresci), wiec nic to nie
    # kosztuje, a warunek pozostaje czytelny. Gdyby progow bylo duzo wiecej
    # albo roznice siegaly miesiecy, warto przeniesc to do zapytania.
    rows = [r for r in rows
            if str(r.get("data_wyd") or "") >= utils.data_start(r.get("podatek"))]
    return [r for r in rows if not _sensowne(r.get("s_streszcz"))]


def _tekst_dla(db: db_core.SupabaseDB, ids: list) -> dict:
    """Pełne teksty TYLKO dla podanych dokumentów (przetwarzana garstka)."""
    if not ids:
        return {}
    rows = db.wykonaj("SELECT id, tekst FROM dokumenty WHERE id = ANY(%s)",
                      (ids,), fetch=True)
    return {r["id"]: r.get("tekst") or "" for r in rows}


def _zapisz(db: db_core.SupabaseDB, r: dict, model: str, wynik: dict) -> None:
    import datetime as dt
    db.wykonaj(
        """
        INSERT INTO streszczenia_auto
            (dokument_id, podatek, model, temat, streszczenie, branze,
             przedmiot, wygenerowano)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (dokument_id, model) DO UPDATE SET
            temat=EXCLUDED.temat, streszczenie=EXCLUDED.streszczenie,
            branze=EXCLUDED.branze, przedmiot=EXCLUDED.przedmiot,
            wygenerowano=EXCLUDED.wygenerowano
        """,
        (r["id"], r["podatek"], model, wynik.get("temat", ""),
         wynik.get("streszczenie", ""), ", ".join(wynik.get("branze") or []),
         "; ".join(wynik.get("przedmioty") or []),
         dt.datetime.now().isoformat(timespec="seconds")),
    )


def _output(klucz: str, wartosc: str) -> None:
    """Zapis do GITHUB_OUTPUT (widoczne dla kolejnych kroków workflow)."""
    plik = os.environ.get("GITHUB_OUTPUT")
    if plik:
        try:
            with open(plik, "a", encoding="utf-8") as f:
                f.write(f"{klucz}={wartosc}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
def main() -> int:
    klucz_api = os.environ.get("OPENROUTER_API_KEY")
    if not klucz_api:
        raise SystemExit("Brak OPENROUTER_API_KEY.")

    db = _polacz()
    _zapewnij_tabele(db)

    lista = _do_streszczenia(db, MODEL)
    print(f"[automat] Model: {MODEL} | próg daty: {DATA_START} | "
          f"do streszczenia: {len(lista)}")
    if not lista:
        print("[automat] Brak nowych interpretacji — nic do zrobienia.")
        _output("pozostalo", "0")
        return 0

    do_zrobienia = lista[:MAKS_NA_RUN]
    # Pełne teksty dobieramy TYLKO dla wsadu (nie dla całej listy braków) —
    # to trzyma egress na uwięzi niezależnie od rozmiaru zaległości.
    teksty = _tekst_dla(db, [r["id"] for r in do_zrobienia])
    for r in do_zrobienia:
        r["tekst"] = teksty.get(r["id"], "")
    zrobione = bledy = 0
    limit_trafiony = False

    for i, r in enumerate(do_zrobienia, start=1):
        try:
            wynik = sopen.streszcz_tekst(
                r.get("tekst") or "", r["sygnatura"], str(r["data_wyd"]),
                api_key=klucz_api, model=MODEL, podatek=r["podatek"],
            )
            _zapisz(db, r, MODEL, wynik)
            zrobione += 1
            print(f"[{i}/{len(do_zrobienia)}] OK  {r['podatek']} {r['sygnatura']}")
        except RuntimeError as e:
            msg = str(e)
            bledy += 1
            print(f"[{i}/{len(do_zrobienia)}] BŁĄD {r['sygnatura']}: {msg}")
            if "429" in msg or "limit" in msg.lower():
                # Limit zapytań — resztę dokończy kolejny przebieg harmonogramu.
                limit_trafiony = True
                break
            if msg.startswith("HTTP 4"):
                # Błąd systemowy (zły model, klucz, konfiguracja) — dotyczy
                # wszystkich pozycji, więc nie ma sensu iść dalej.
                print("[automat] Błąd konfiguracji/żądania — przerywam "
                      "(sprawdź model, klucz i sekrety).")
                break
            # inny błąd pojedynczej pozycji — pomiń i próbuj dalej
        if i < len(do_zrobienia):
            time.sleep(PRZERWA_S)

    pozostalo = len(lista) - zrobione
    print(f"[automat] Zapisano: {zrobione} | błędy: {bledy} | pozostało: {pozostalo}")
    if limit_trafiony:
        print("[automat] Trafiono limit — kolejne uruchomienie harmonogramu "
              "dokończy pozostałe pozycje (idempotentnie).")
    _output("pozostalo", str(pozostalo))
    # Zawsze kończymy sukcesem (0) — limit to normalny stan, nie awaria builda.
    return 0


if __name__ == "__main__":
    sys.exit(main())
