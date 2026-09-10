# -*- coding: utf-8 -*-
"""
auth.py — Konta użytkowników, role i uprawnienia (Skaner Doradca).
NIEZALEŻNY od interfejsu (logika + baza), używany przez app.py i moduły UI.

Model:
  • Konto zaszyte DORADCA (hasło z st.secrets) — superadmin awaryjny,
    niezależny od bazy, zwolniony z reguły domeny.
  • Konta bazodanowe: adres @doradca.lublin.pl, rola 'admin' | 'user',
    hasło bcrypt, status 'oczekuje' | 'aktywne' | 'nieaktywne'.
  • Aktywacja 6-cyfrowym kodem (hash bcrypt, ważny 24 h), wpisywanym przy
    pierwszym logowaniu wraz z ustawieniem hasła.

Uprawnienia modułowe: patrz UPRAWNIENIA — mapa (klucz modułu -> role).
"""

from __future__ import annotations

import datetime as dt
import re
import secrets as _secrets

import bcrypt

import archiwum_supabase

DOMENA = "@doradca.lublin.pl"
KOD_WAZNOSC_H = 24
KOD_MAKS_PROB = 5
ROLE = ("admin", "user")

# Mapa uprawnień: klucz modułu -> zbiór ról z dostępem.
# 'admin' obejmuje też konto zaszyte DORADCA (superadmin).
# Klucze odpowiadają modułom z menu (numer startowy pozycji).
UPRAWNIENIA = {
    "1": {"admin"},                     # Ściągacz
    "2": {"admin", "user"},             # Archiwum
    "3": {"admin", "user"},             # Analiza Wskaźnikowa
    "4": {"admin", "user"},             # Wyroki
    "5": {"admin", "user"},             # Zestawienie Tygodniowe (user: tylko odczyt)
    "6": {"admin", "user"},             # Zestawienie Automat
    "7": {"admin", "user"},             # Monitoring (user: tylko własne)
    "8": {"admin", "user"},             # Wyszukiwarka
    "9": {"admin", "user"},             # Aktywność systemu (wszyscy)
    "10": {"admin", "user"},            # Mój panel (wszyscy)
    "11": {"admin"},                    # Ustawienia Systemu
    "12": {"admin"},                    # Uzupełnianie klasyfikacji
    "13": {"admin", "user"},
}

# Uprawnienia szczegółowe (nie-modułowe), sprawdzane wewnątrz modułów:
#   'zestawienie_wgrywanie' — wgrywanie plików DOCX w module 5 (tylko admin)
#   'monitoring_wszystkie'  — wgląd/kasowanie cudzych alertów (tylko admin)
#   'zarzadzanie_kontami'   — panel kont w Ustawieniach (tylko admin)
UPRAWNIENIA_SZCZEGOLOWE = {
    "zestawienie_wgrywanie": {"admin"},
    "monitoring_wszystkie": {"admin"},
    "zarzadzanie_kontami": {"admin"},
}


# ---------------------------------------------------------------------------
_db_zewnetrzna = None


def _db():
    """
    Połączenie z bazą — z Streamlita albo ze zmiennych środowiskowych.

    DLACZEGO DWIE DROGI
      Ten moduł trzyma CAŁĄ logikę kont: zakładanie, kody aktywacyjne, ich
      ważność, limit prób, wymogi hasła, role. Dopóki sięgał wyłącznie przez
      `archiwum_supabase` (czyli przez `st.secrets` i `st.cache_resource`), był
      użyteczny tylko wewnątrz Streamlita. Nowy interfejs w Dockerze musiałby
      więc powielić te reguły u siebie — a dwie kopie polityki haseł rozjadą się
      przy pierwszej zmianie i nikt tego nie zauważy, bo obie „działają".

      Poza Streamlitem `st.secrets` rzuca StreamlitSecretNotFound. Wtedy
      budujemy połączenie z tych samych zmiennych SUPABASE_*, których używają
      skrypty wsadowe i kontenery. Wewnątrz Streamlita nic się nie zmienia —
      pierwsza droga jest nadal pierwsza.
    """
    global _db_zewnetrzna

    try:
        return archiwum_supabase._get_db()
    except Exception:
        pass  # brak Streamlita albo jego sekretów — próbujemy środowiska

    if _db_zewnetrzna is None:
        import os
        import db_core
        brakujace = [k for k in ("SUPABASE_HOST", "SUPABASE_PASSWORD")
                     if not os.environ.get(k)]
        if brakujace:
            raise RuntimeError(
                "Brak konfiguracji bazy: ani sekretów Streamlit, ani zmiennych "
                + ", ".join(brakujace) + "."
            )
        _db_zewnetrzna = db_core.SupabaseDB({
            "host":     os.environ["SUPABASE_HOST"],
            "port":     os.environ.get("SUPABASE_PORT", "5432"),
            "database": os.environ.get("SUPABASE_DB", "postgres"),
            "user":     os.environ.get("SUPABASE_USER", "postgres"),
            "password": os.environ["SUPABASE_PASSWORD"],
            "sslmode":  os.environ.get("SUPABASE_SSLMODE", "require"),
        })
    return _db_zewnetrzna


def zapewnij_tabele() -> None:
    _db().wykonaj(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,   -- @doradca.lublin.pl
            rola          TEXT NOT NULL DEFAULT 'user',
            haslo_hash    TEXT DEFAULT '',        -- bcrypt; puste do aktywacji
            status        TEXT NOT NULL DEFAULT 'oczekuje',
            kod_hash      TEXT DEFAULT '',        -- bcrypt kodu aktywacyjnego
            kod_wazny_do  TEXT DEFAULT '',        -- ISO; po tym czasie kod nieważny
            kod_proby     INTEGER DEFAULT 0,      -- licznik błędnych prób kodu
            utworzono     TEXT NOT NULL,
            aktywowano    TEXT DEFAULT ''
        )
        """
    )
    # Wiersz techniczny dla konta zaszytego DORADCA — samo logowanie z niego
    # NIE korzysta (idzie przez st.secrets, patrz _zaloguj_doradca w app.py),
    # ale parowanie wtyczki (dostep_wtyczki.py) i wtyczka-auth po stronie
    # Supabase wymagają realnego wiersza w users (FK z wtyczka_kody, sprawdzenie
    # rola/status przy wydawaniu i weryfikacji tokenu). ON CONFLICT DO NOTHING,
    # żeby nie nadpisywać roli/statusu, gdyby ktoś kiedyś ręcznie je zmienił.
    # lista_uzytkownikow() celowo pomija ten wiersz w panelu kont.
    _db().wykonaj(
        """
        INSERT INTO users (email, rola, haslo_hash, status, utworzono)
        VALUES ('DORADCA', 'admin', '', 'aktywne', %s)
        ON CONFLICT (email) DO NOTHING
        """,
        (dt.datetime.now(dt.timezone.utc).isoformat(),),
    )


# ---------------------------------------------------------------------------
# WALIDACJE
# ---------------------------------------------------------------------------
def email_poprawny(email: str) -> bool:
    e = (email or "").strip().lower()
    return e.endswith(DOMENA) and re.match(r"^[^@\s]+" + re.escape(DOMENA) + r"$", e) is not None


def haslo_wymogi(haslo: str) -> str | None:
    """Zwraca komunikat błędu albo None, gdy hasło spełnia rygor:
    min. 8 znaków, co najmniej jedna cyfra i jeden znak specjalny."""
    h = haslo or ""
    if len(h) < 8:
        return "Hasło musi mieć co najmniej 8 znaków."
    if not re.search(r"\d", h):
        return "Hasło musi zawierać co najmniej jedną cyfrę."
    if not re.search(r"[^A-Za-z0-9]", h):
        return "Hasło musi zawierać co najmniej jeden znak specjalny."
    return None


# ---------------------------------------------------------------------------
# HASH
# ---------------------------------------------------------------------------
def _hash(txt: str) -> str:
    return bcrypt.hashpw(txt.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _sprawdz_hash(txt: str, h: str) -> bool:
    if not h:
        return False
    try:
        return bcrypt.checkpw(txt.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ODCZYT KONT
# ---------------------------------------------------------------------------
def pobierz_uzytkownika(email: str) -> dict | None:
    rows = _db().wykonaj(
        "SELECT * FROM users WHERE email = %s", ((email or "").strip().lower(),),
        fetch=True,
    )
    return rows[0] if rows else None


def lista_uzytkownikow() -> list[dict]:
    # DORADCA ma tu wiersz techniczny (patrz zapewnij_tabele) tylko po to,
    # żeby dało się z nim sparować wtyczkę — w panelu kont ma pozostać
    # niewidoczny, tak jak zapowiada ustawienia_systemu.py.
    return _db().wykonaj(
        "SELECT id, email, rola, status, utworzono, aktywowano "
        "FROM users WHERE email <> 'DORADCA' ORDER BY email", fetch=True,
    )


# ---------------------------------------------------------------------------
# TWORZENIE KONTA + KOD AKTYWACYJNY
# ---------------------------------------------------------------------------
def _nowy_kod() -> str:
    return f"{_secrets.randbelow(1000000):06d}"


def utworz_konto(email: str, rola: str) -> str:
    """Tworzy konto w stanie 'oczekuje' i zwraca 6-cyfrowy kod (do wysyłki
    mailem). Rzuca ValueError przy złym adresie/roli lub istniejącym koncie."""
    e = (email or "").strip().lower()
    if not email_poprawny(e):
        raise ValueError(f"Adres musi kończyć się na {DOMENA}.")
    if rola not in ROLE:
        raise ValueError("Nieprawidłowa rola.")
    if pobierz_uzytkownika(e):
        raise ValueError("Konto o tym adresie już istnieje.")

    kod = _nowy_kod()
    wazny_do = (dt.datetime.now() + dt.timedelta(hours=KOD_WAZNOSC_H)).isoformat(
        timespec="seconds")
    _db().wykonaj(
        """INSERT INTO users (email, rola, status, kod_hash, kod_wazny_do,
                              kod_proby, utworzono)
           VALUES (%s,%s,'oczekuje',%s,%s,0,%s)""",
        (e, rola, _hash(kod), wazny_do,
         dt.datetime.now().isoformat(timespec="seconds")),
    )
    return kod


def wygeneruj_nowy_kod(email: str) -> str:
    """Reset/ponowna aktywacja: nowy kod, konto wraca do stanu 'oczekuje',
    hasło czyszczone (użytkownik ustawi nowe). Zwraca kod do wysyłki."""
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u:
        raise ValueError("Nie ma konta o tym adresie.")
    kod = _nowy_kod()
    wazny_do = (dt.datetime.now() + dt.timedelta(hours=KOD_WAZNOSC_H)).isoformat(
        timespec="seconds")
    _db().wykonaj(
        """UPDATE users SET status='oczekuje', haslo_hash='', kod_hash=%s,
                            kod_wazny_do=%s, kod_proby=0 WHERE email=%s""",
        (_hash(kod), wazny_do, e),
    )
    return kod


# ---------------------------------------------------------------------------
# AKTYWACJA (pierwsze logowanie / po resecie)
# ---------------------------------------------------------------------------
def aktywuj(email: str, kod: str, nowe_haslo: str) -> None:
    """Weryfikuje kod (ważność + próby) i ustawia hasło. Rzuca ValueError
    z czytelnym komunikatem przy każdym niepowodzeniu."""
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u:
        raise ValueError("Nie ma konta o tym adresie.")
    if u["status"] == "nieaktywne":
        raise ValueError("Konto jest zablokowane — skontaktuj się z administratorem.")
    if u["status"] == "aktywne":
        raise ValueError("Konto jest już aktywne — użyj zwykłego logowania.")

    if int(u.get("kod_proby") or 0) >= KOD_MAKS_PROB:
        raise ValueError("Zbyt wiele błędnych prób. Poproś administratora o nowy kod.")

    wazny_do = u.get("kod_wazny_do") or ""
    if not wazny_do or dt.datetime.fromisoformat(wazny_do) < dt.datetime.now():
        raise ValueError("Kod aktywacyjny wygasł. Poproś administratora o nowy.")

    if not _sprawdz_hash(kod.strip(), u.get("kod_hash") or ""):
        _db().wykonaj("UPDATE users SET kod_proby = kod_proby + 1 WHERE email=%s", (e,))
        raise ValueError("Błędny kod aktywacyjny.")

    blad = haslo_wymogi(nowe_haslo)
    if blad:
        raise ValueError(blad)

    _db().wykonaj(
        """UPDATE users SET haslo_hash=%s, status='aktywne', kod_hash='',
                            kod_wazny_do='', kod_proby=0, aktywowano=%s
           WHERE email=%s""",
        (_hash(nowe_haslo), dt.datetime.now().isoformat(timespec="seconds"), e),
    )


# ---------------------------------------------------------------------------
# LOGOWANIE / STATUS / UPRAWNIENIA
# ---------------------------------------------------------------------------
def zaloguj(email: str, haslo: str) -> dict | None:
    """Zwraca sesję {'email','rola','superadmin'} albo None."""
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u or u["status"] != "aktywne":
        return None
    if not _sprawdz_hash(haslo, u.get("haslo_hash") or ""):
        return None
    return {"email": e, "rola": u["rola"], "superadmin": u["rola"] == "admin"}


def zmien_haslo(email: str, stare: str, nowe: str) -> None:
    """Zmiana własnego hasła przez zalogowanego użytkownika. Weryfikuje stare,
    waliduje i hashuje nowe. Rzuca ValueError z komunikatem przy błędzie."""
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u or u["status"] != "aktywne":
        raise ValueError("Konto nieaktywne lub nie istnieje.")
    if not _sprawdz_hash(stare, u.get("haslo_hash") or ""):
        raise ValueError("Obecne hasło jest nieprawidłowe.")
    blad = haslo_wymogi(nowe)
    if blad:
        raise ValueError(blad)
    if _sprawdz_hash(nowe, u.get("haslo_hash") or ""):
        raise ValueError("Nowe hasło musi różnić się od obecnego.")
    _db().wykonaj("UPDATE users SET haslo_hash=%s WHERE email=%s",
                  (_hash(nowe), e))


def dezaktywuj(email: str) -> None:
    _db().wykonaj("UPDATE users SET status='nieaktywne' WHERE email=%s",
                  ((email or "").strip().lower(),))


def aktywuj_ponownie(email: str) -> None:
    """Odblokowanie wcześniej zdezaktywowanego konta (bez zmiany hasła)."""
    _db().wykonaj(
        "UPDATE users SET status='aktywne' WHERE email=%s AND haslo_hash <> ''",
        ((email or "").strip().lower(),),
    )


def ma_dostep(rola: str, modul: str) -> bool:
    # Administrator (w tym konto zaszyte DORADCA) ma dostęp do KAŻDEGO modułu
    # z definicji — niezależnie od mapy, żeby nie dało się go przypadkiem
    # zablokować (np. po zmianie numeracji modułów).
    if rola == "admin":
        return True
    return rola in UPRAWNIENIA.get(modul, set())


def ma_uprawnienie(rola: str, nazwa: str) -> bool:
    if rola == "admin":
        return True
    return rola in UPRAWNIENIA_SZCZEGOLOWE.get(nazwa, set())
