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
    # Kod odzyskiwania — dokładany osobno, bo tabela istnieje już w bazach
    # produkcyjnych. CREATE TABLE IF NOT EXISTS nie dodaje kolumn do istniejącej
    # tabeli, więc bez tych ALTER-ów nowa funkcja działałaby wyłącznie na
    # świeżo założonej bazie i nikt by tego nie zauważył aż do pierwszego użycia.
    for kolumna, typ in (("kod_odzysk_hash", "TEXT DEFAULT ''"),
                         ("kod_odzysk_utworzono", "TEXT DEFAULT ''"),
                         ("kod_odzysk_proby", "INTEGER DEFAULT 0")):
        _db().wykonaj(
            f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {kolumna} {typ}")

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


# ---------------------------------------------------------------------------
# UPRAWNIENIA PRZYPISANE OSOBIE
#
# Dotychczasowy model jest wylacznie ROLOWY (UPRAWNIENIA / UPRAWNIENIA_SZCZEGOLOWE
# wyzej): uprawnienie wynika z roli, a rola ma tylko dwie wartosci. Sa jednak
# zadania, ktore chce sie powierzyc KONKRETNEJ osobie, nie dajac jej przy tym
# praw administratora — na przyklad wgrywanie Dziennika Gazety Prawnej.
#
# Te dwa modele celowo sie nie mieszaja: `ma_uprawnienie(rola, nazwa)` odpowiada
# na pytanie „czy ta ROLA moze", a `ma_uprawnienie_osobiste(email, nazwa)"
# na „czy TEN CZLOWIEK moze". Administrator ma wszystkie osobiste z urzedu —
# inaczej trzeba by mu je nadawac pojedynczo, a to zaprzeczenie roli admina.
# ---------------------------------------------------------------------------
UPRAWNIENIA_OSOBISTE = {
    "dgp_wgrywanie": "Wgrywanie Dziennika Gazety Prawnej",
    "ai_prompt": "Pytanie do Claude'a z dymka na stronie dokumentu",
}

# ---------------------------------------------------------------------------
# BIURA (jednostki organizacyjne)
#
# Lista mieszka TUTAJ, a nie w ograniczeniu CHECK w bazie: nazwa oddzialu
# zmienia sie czesciej niz schemat, a przy pieciu wartosciach migracja za
# kazda literowka byloby placeniem za sztywnosc, ktorej nikt nie potrzebuje.
#
# Biuro NIE JEST uprawnieniem. Mowi, gdzie ktos pracuje; o tym, co wolno,
# rozstrzyga UPRAWNIENIA_OSOBISTE. Te dwie rzeczy trzymamy osobno, bo pierwsza
# osoba z wyjatkiem od reguly „biuro X moze Y" — a taka zawsze sie znajdzie —
# kazalaby ten skrot rozplatywac wstecz.
# ---------------------------------------------------------------------------
BIURA = [
    "Biuro Doradztwa Podatkowego, Strategii i Rozwoju",
    "Zarzad i Administracja",
    "Biuro Badania Sprawozdan Finansowych i Innych Uslug Bieglego Rewidenta",
    "Oddzial w Chelmie",
    "Biuro w Radzyniu Podlaskim",
]


def ustaw_biuro(email: str, biuro: str) -> None:
    """
    Przypisuje konto do jednostki. Pusty lancuch odpina.

    Nazwa spoza listy jest bledem, a nie zapisem do poprawienia pozniej:
    literowka w nazwie oddzialu tworzy jednostke-widmo, do ktorej nikt inny
    nigdy nie trafi, a filtr „kto jest w Chelmie" po cichu ja pominie.
    """
    b = (biuro or "").strip()
    if b and b not in BIURA:
        raise ValueError("Nieznane biuro: %s" % b)
    e = (email or "").strip().lower()
    if not pobierz_uzytkownika(e):
        raise ValueError("Konto nie istnieje.")
    _db().wykonaj("UPDATE users SET biuro = %s WHERE lower(email) = %s", (b, e))


def biuro_uzytkownika(email: str) -> str:
    w = _db().wykonaj(
        "SELECT biuro FROM users WHERE lower(email) = %s LIMIT 1",
        ((email or "").strip().lower(),), fetch=True) or []
    return (w[0]["biuro"] if w else "") or ""


def nadaj_uprawnienie(email: str, uprawnienie: str, nadal: str = "") -> None:
    """Nadaje uprawnienie osobiste. Ponowne nadanie nie jest bledem."""
    e = (email or "").strip().lower()
    if uprawnienie not in UPRAWNIENIA_OSOBISTE:
        raise ValueError("Nieznane uprawnienie: %s" % uprawnienie)
    if not pobierz_uzytkownika(e):
        raise ValueError("Konto nie istnieje.")
    _db().wykonaj(
        """INSERT INTO uprawnienia_uzytkownika (email, uprawnienie, nadal)
           VALUES (%s, %s, %s)
           ON CONFLICT (email, uprawnienie) DO NOTHING""",
        (e, uprawnienie, (nadal or "").strip().lower()),
    )


def odbierz_uprawnienie(email: str, uprawnienie: str) -> None:
    _db().wykonaj(
        "DELETE FROM uprawnienia_uzytkownika "
        "WHERE lower(email) = %s AND uprawnienie = %s",
        ((email or "").strip().lower(), uprawnienie),
    )


def uprawnienia_osobiste(email: str) -> set:
    """Zbior uprawnien nadanych temu kontu (bez tych z roli)."""
    w = _db().wykonaj(
        "SELECT uprawnienie FROM uprawnienia_uzytkownika WHERE lower(email) = %s",
        ((email or "").strip().lower(),), fetch=True) or []
    return {r["uprawnienie"] for r in w}


def ma_uprawnienie_osobiste(email: str, uprawnienie: str, rola: str = "") -> bool:
    """
    Czy TEN CZLOWIEK moze zrobic dana rzecz.

    Administrator moze wszystko z urzedu — inaczej trzeba by nadawac mu kazde
    uprawnienie osobno, co przeczy sensowi tej roli. Poza tym rola sprawdzana
    jest tu tylko wtedy, gdy wywolujacy ja poda; brak argumentu oznacza
    sprawdzenie samego nadania.
    """
    if rola == "admin":
        return True
    e = (email or "").strip().lower()
    if e == "doradca":          # konto zaszyte ma prawa administratora
        return True
    return uprawnienie in uprawnienia_osobiste(e)


def kto_ma_uprawnienie(uprawnienie: str) -> list:
    """Lista adresow z danym uprawnieniem — do panelu zarzadzania."""
    w = _db().wykonaj(
        "SELECT email FROM uprawnienia_uzytkownika WHERE uprawnienie = %s "
        "ORDER BY email", (uprawnienie,), fetch=True) or []
    return [r["email"] for r in w]


def zmien_role(email: str, nowa_rola: str) -> None:
    """
    Nadanie albo odebranie uprawnień administratora.

    Rola 'admin' daje DOKŁADNIE te same możliwości co konto zaszyte DORADCA —
    patrz UPRAWNIENIA i UPRAWNIENIA_SZCZEGOLOWE wyżej: obie mapy znają tylko
    'admin' i 'user', a DORADCA jest w nich traktowane jako 'admin'. Nadanie
    komuś tej roli nie jest więc „prawie adminem" i tak trzeba je traktować.

    Wiersza DORADCA nie ruszamy. To konto techniczne: loguje się przez
    st.secrets, a jego wiersz w users istnieje wyłącznie po to, by wtyczka
    miała się z czym sparować. Odebranie mu roli 'admin' zerwałoby parowanie
    urządzeń, nie odbierając nikomu żadnego realnego dostępu.
    """
    e = (email or "").strip().lower()
    if e == "doradca":
        raise ValueError("Konta DORADCA nie można zmieniać.")
    if nowa_rola not in ROLE:
        raise ValueError(f"Nieznana rola: {nowa_rola}")
    u = pobierz_uzytkownika(e)
    if not u:
        raise ValueError("Konto nie istnieje.")

    # Ostatni admin poza DORADCA — ostrzegamy, ale nie blokujemy: DORADCA zawsze
    # może nadać rolę z powrotem, więc nie da się tym zamknąć drzwi na amen.
    _db().wykonaj("UPDATE users SET rola=%s WHERE lower(email)=%s", (nowa_rola, e))


# ---------------------------------------------------------------------------
# KOD ODZYSKIWANIA — awaryjna droga, gdy poczta zawiedzie
#
# Kod jest DRUGĄ drogą do ustawienia hasła, obok kodu wysyłanego mailem. Powód
# istnienia: gdy poczta przestanie działać (wygasłe hasło aplikacji Google,
# blokada, awaria), konto administratora byłoby nie do odzyskania w ogóle.
#
# TRZY OGRANICZENIA, KTÓRE CZYNIĄ TO BEZPIECZNYM
#   1. Tylko dla roli 'admin'. Zwykły użytkownik odzyskuje hasło mailem, a gdy
#      poczta leży — prosi administratora. Mniej stałych sekretów w systemie.
#   2. Trzymany jako hash bcrypt, nie jawnie. Pokazujemy go RAZ, w chwili
#      wygenerowania. Wyciek bazy nie daje więc gotowego klucza do kont admina —
#      inaczej kod byłby słabszym odpowiednikiem hasła, zapisanym obok niego.
#   3. Jednorazowy i z limitem prób. Po użyciu jest kasowany, po 5 błędnych
#      próbach unieważniany — tak samo jak kod z maila.
# ---------------------------------------------------------------------------
KOD_ODZYSK_ZNAKOW = 12


def _nowy_kod_odzysk() -> str:
    """Same cyfry, w grupach po cztery — łatwiej przepisać z kartki."""
    cyfry = "".join(_secrets.choice("0123456789") for _ in range(KOD_ODZYSK_ZNAKOW))
    return "-".join(cyfry[i:i + 4] for i in range(0, KOD_ODZYSK_ZNAKOW, 4))


def nowy_kod_odzyskiwania(email: str) -> str:
    """
    Generuje kod dla konta administratora i zwraca go JAWNIE — jedyny raz.

    Wywołujący ma obowiązek pokazać go użytkownikowi od razu; w bazie ląduje
    wyłącznie hash. Wygenerowanie nowego unieważnia poprzedni.
    """
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u:
        raise ValueError("Konto nie istnieje.")
    if u["rola"] != "admin":
        raise ValueError("Kod odzyskiwania przysługuje wyłącznie kontom administratora.")

    kod = _nowy_kod_odzysk()
    _db().wykonaj(
        """UPDATE users SET kod_odzysk_hash=%s, kod_odzysk_utworzono=%s,
                            kod_odzysk_proby=0
           WHERE lower(email)=%s""",
        (_hash(kod), dt.datetime.now(dt.timezone.utc).isoformat(), e),
    )
    return kod


def ma_kod_odzyskiwania(email: str) -> str:
    """Data wygenerowania kodu albo '' — do pokazania w ustawieniach profilu."""
    u = pobierz_uzytkownika((email or "").strip().lower())
    if not u or not (u.get("kod_odzysk_hash") or ""):
        return ""
    return (u.get("kod_odzysk_utworzono") or "")[:10]


def odzyskaj_haslo(email: str, kod: str, nowe_haslo: str) -> None:
    """
    Ustawia nowe hasło na podstawie kodu odzyskiwania. Rzuca ValueError.

    Po udanym użyciu kod jest kasowany — jednorazowość jest tu istotna, bo
    ten kod nie ma daty ważności i inaczej byłby stałym drugim hasłem.
    """
    e = (email or "").strip().lower()
    u = pobierz_uzytkownika(e)
    if not u or u["status"] != "aktywne":
        raise ValueError("Konto nieaktywne lub nie istnieje.")
    if u["rola"] != "admin":
        raise ValueError("Ta droga jest dostępna wyłącznie dla kont administratora.")

    zapisany = u.get("kod_odzysk_hash") or ""
    if not zapisany:
        raise ValueError("To konto nie ma kodu odzyskiwania.")

    if int(u.get("kod_odzysk_proby") or 0) >= KOD_MAKS_PROB:
        _db().wykonaj("UPDATE users SET kod_odzysk_hash='' WHERE lower(email)=%s", (e,))
        raise ValueError("Przekroczono liczbę prób — kod został unieważniony. "
                         "Poproś innego administratora o nowy.")

    # Znormalizowane porównanie: użytkownik przepisuje z kartki i myślniki
    # albo spacje nie powinny decydować o powodzeniu.
    podany = re.sub(r"[^0-9]", "", kod or "")
    wzorzec = "-".join(podany[i:i + 4] for i in range(0, len(podany), 4))

    if not _sprawdz_hash(wzorzec, zapisany):
        _db().wykonaj(
            "UPDATE users SET kod_odzysk_proby = COALESCE(kod_odzysk_proby,0)+1 "
            "WHERE lower(email)=%s", (e,))
        raise ValueError("Nieprawidłowy kod odzyskiwania.")

    blad = haslo_wymogi(nowe_haslo)
    if blad:
        raise ValueError(blad)

    _db().wykonaj(
        """UPDATE users SET haslo_hash=%s, kod_odzysk_hash='',
                            kod_odzysk_utworzono='', kod_odzysk_proby=0
           WHERE lower(email)=%s""",
        (_hash(nowe_haslo), e),
    )


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
