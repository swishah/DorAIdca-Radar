# -*- coding: utf-8 -*-
"""
MODUŁ 13: Dostęp wtyczki — Skaner Doradca.

Generuje jednorazowe kody parowania i pokazuje urządzenia, którym wydano
token. Zastępuje wspólny sekret, który dotąd był identyczny dla wszystkich
i którego nie dało się odebrać jednej osobie.

CO TU SIĘ DZIEJE
    Zalogowany prosi o kod, wkleja go raz w ustawienia wtyczki, a wtyczka
    wymienia go w Edge Function na token przypisany do jego konta. Hasło nigdy
    nie trafia do rozszerzenia.

CZEGO TU NIE MA
    Kodu ani tokenu w postaci jawnej — baza trzyma wyłącznie skróty SHA-256.
    Kod pokazujemy raz, w chwili wygenerowania; potem nie ma go skąd odczytać,
    także dla administratora.

UPRAWNIENIA
    Zwykły użytkownik widzi i odwołuje własne urządzenia. Administrator widzi
    wszystkie — bo to on odbiera dostęp osobie, która odeszła z firmy.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import streamlit as st

import archiwum_supabase
import auth

# Kod ma być przepisywalny z ekranu na klawiaturę. Znaki mylące (0/O, 1/I/L)
# są wyłączone, bo pomyłka przy przepisywaniu wygląda jak odrzucony kod.
ZNAKI = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
DLUGOSC_KODU = 12
WAZNOSC_MINUT = 15


def _db():
    return archiwum_supabase._get_db()


def _skrot(tekst: str) -> str:
    return hashlib.sha256(tekst.encode("utf-8")).hexdigest()


def _nowy_kod() -> str:
    """Kod parowania w postaci XXXX-XXXX-XXXX."""
    znaki = "".join(secrets.choice(ZNAKI) for _ in range(DLUGOSC_KODU))
    return "-".join(znaki[i:i + 4] for i in range(0, DLUGOSC_KODU, 4))


# ---------------------------------------------------------------------------
# OPERACJE
# ---------------------------------------------------------------------------

def wygeneruj_kod(email: str) -> str:
    """Nowy kod dla użytkownika. Poprzednie, niezużyte kody unieważniamy.

    Bez tego kilka kodów naraz byłoby ważnych jednocześnie — a kod widziany
    kiedyś na ekranie w cudzej obecności powinien przestać działać, gdy
    poprosisz o nowy.
    """
    db = _db()
    db.wykonaj(
        "UPDATE wtyczka_kody SET zuzyty = TRUE "
        "WHERE email = %s AND zuzyty = FALSE",
        (email,),
    )

    kod = _nowy_kod()
    wazny_do = datetime.now(timezone.utc) + timedelta(minutes=WAZNOSC_MINUT)
    db.wykonaj(
        "INSERT INTO wtyczka_kody (email, kod_hash, wazny_do) "
        "VALUES (%s, %s, %s)",
        (email, _skrot(kod.replace("-", "")), wazny_do.isoformat()),
    )
    return kod


def urzadzenia(email: str | None) -> list[dict]:
    """Urządzenia z tokenem. `email=None` oznacza wszystkie (tylko admin)."""
    if email:
        return _db().wykonaj(
            "SELECT id, email, rola, urzadzenie, utworzono, ostatnie_uzycie, "
            "aktywny FROM wtyczka_urzadzenia WHERE email = %s "
            "ORDER BY aktywny DESC, utworzono DESC",
            (email,), fetch=True,
        )
    return _db().wykonaj(
        "SELECT id, email, rola, urzadzenie, utworzono, ostatnie_uzycie, "
        "aktywny FROM wtyczka_urzadzenia "
        "ORDER BY aktywny DESC, email, utworzono DESC",
        fetch=True,
    )


def odwolaj(token_id: int, email: str | None) -> None:
    """Unieważnienie tokenu.

    Warunek `email` w SQL, a nie tylko w interfejsie: gdyby ktoś podmienił
    identyfikator w żądaniu, nie odwoła cudzego urządzenia.
    """
    if email:
        _db().wykonaj(
            "UPDATE wtyczka_tokeny SET aktywny = FALSE, odwolano = now() "
            "WHERE id = %s AND email = %s",
            (token_id, email),
        )
    else:
        _db().wykonaj(
            "UPDATE wtyczka_tokeny SET aktywny = FALSE, odwolano = now() "
            "WHERE id = %s",
            (token_id,),
        )


# ---------------------------------------------------------------------------
# INTERFEJS
# ---------------------------------------------------------------------------

def _skrot_daty(wartosc) -> str:
    if not wartosc:
        return "—"
    tekst = str(wartosc)
    return tekst[:16].replace("T", " ")


def pokaz_dostep_wtyczki() -> None:
    st.header("🔑 Dostęp wtyczki")

    # Aplikacja trzyma adres pod kluczem `user_email`, a rolę wylicza
    # z `superadmin` albo `rola` — tak samo jak routing w app.py.
    email = st.session_state.get("user_email") or ""
    rola = ("admin" if (st.session_state.get("superadmin")
                        or st.session_state.get("rola") == "admin") else "user")

    if not email:
        st.warning("Ta sekcja wymaga zalogowania.")
        return

    # KONTO DORADCA ma wiersz techniczny w `users` (zapewnij_tabele w auth.py)
    # wyłącznie po to, żeby to parowanie miało się do czego odwołać (FK z
    # wtyczka_kody, sprawdzenie rola/status przy wydawaniu i weryfikacji
    # tokenu w Edge Function). Token wystawiony w ten sposób jest wspólny dla
    # każdego, kto zna hasło DORADCA z Secrets — jeśli z konta korzysta więcej
    # niż jedna osoba, odwołanie urządzenia poniżej odwołuje WSZYSTKIE
    # sparowane pod DORADCA, nie tylko jedno.
    if email == "DORADCA":
        st.info(
            "Parujesz konto zaszyte DORADCA — token będzie wspólny dla "
            "każdego, kto loguje się tym hasłem. Jeśli z konta korzysta "
            "więcej osób, rozważ zamiast tego konta bazodanowe "
            "(@doradca.lublin.pl) dla każdej z nich.", icon="ℹ️")

    st.caption(
        "Wtyczka rozpoznaje Cię po tokenie przypisanym do tego konta. Token "
        "powstaje z jednorazowego kodu, który wklejasz raz w jej ustawieniach "
        "— hasło nigdy nie trafia do rozszerzenia."
    )

    # ── kod parowania ──
    st.subheader("Sparuj nowe urządzenie")

    if st.button("Wygeneruj kod", type="primary"):
        st.session_state["kod_parowania"] = wygeneruj_kod(email)
        st.session_state["kod_czas"] = datetime.now(timezone.utc)

    kod = st.session_state.get("kod_parowania")
    if kod:
        st.code(kod, language=None)
        st.info(
            f"Kod jest ważny {WAZNOSC_MINUT} minut i zadziała **jeden raz**. "
            "Wklej go w ustawieniach wtyczki, sekcja „Dostęp do bazy”. "
            "Po zamknięciu tej strony nie da się go odczytać ponownie — "
            "wygeneruj wtedy nowy."
        )
        st.caption(
            "Wygenerowanie nowego kodu unieważnia poprzedni, jeszcze niezużyty."
        )

    st.divider()

    # ── urządzenia ──
    st.subheader("Sparowane urządzenia")

    widzi_wszystkie = rola == "admin"
    if widzi_wszystkie:
        pokaz_wszystkie = st.checkbox(
            "Pokaż urządzenia wszystkich użytkowników", value=False,
            help="Administrator odbiera dostęp osobie, która odeszła z firmy.",
        )
    else:
        pokaz_wszystkie = False

    lista = urzadzenia(None if pokaz_wszystkie else email)

    if not lista:
        st.info("Nie ma jeszcze sparowanych urządzeń.")
        return

    for wpis in lista:
        kolumny = st.columns([3, 2, 2, 1])

        opis = wpis["urzadzenie"] or "(bez opisu)"
        if pokaz_wszystkie:
            opis = f"{wpis['email']} — {opis}"
        kolumny[0].write(f"**{opis}**" if wpis["aktywny"] else f"~~{opis}~~")

        kolumny[1].caption(f"sparowano: {_skrot_daty(wpis['utworzono'])}")
        kolumny[2].caption(f"ostatnio: {_skrot_daty(wpis['ostatnie_uzycie'])}")

        if wpis["aktywny"]:
            if kolumny[3].button("Odwołaj", key=f"odw_{wpis['id']}"):
                odwolaj(wpis["id"], None if widzi_wszystkie else email)
                st.success("Token odwołany. Wtyczka na tym urządzeniu "
                           "straci dostęp przy najbliższym sprawdzeniu.")
                st.rerun()
        else:
            kolumny[3].caption("odwołany")

    st.caption(
        "Odwołanie działa natychmiast po stronie bazy. Wtyczka na odwołanym "
        "urządzeniu może jeszcze przez chwilę korzystać z zapamiętanego "
        "wyniku sprawdzenia — najdalej do końca dnia."
    )
