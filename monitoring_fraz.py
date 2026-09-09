# -*- coding: utf-8 -*-
"""
monitoring_fraz.py — MONITORING FRAZ + POWIADOMIENIA E-MAIL (GitHub Actions).
Niezależny od Streamlit. Konfiguracja z os.environ.

Co robi przy każdym uruchomieniu:
  1. Pobiera aktywne obserwowane frazy (tabela obserwowane_frazy — dodawane
     w aplikacji, moduł „Monitoring Fraz”).
  2. Szuka dopasowań w dokumentach pobranych w ostatnich OKNO_DNI dniach
     (ILIKE na treści i sygnaturze — łapie odmiany: „ciepłownictw” trafi
     „ciepłownictwa”, „ciepłownictwem” itd.).
  3. Pomija pary (fraza, dokument), o których już wysłano powiadomienie
     (tabela monitoring_wyslane) — IDEMPOTENTNY, można odpalać wielokrotnie.
  4. Grupuje nowe trafienia per adres e-mail i wysyła JEDEN zbiorczy mail
     na adres, po czym zapisuje pary jako wysłane.

POSTAĆ WIADOMOŚCI
  Układ BLOKOWY — bez tabeli. Grupowanie po frazie/branży/przedmiocie, pod nim
  kolejne interpretacje: sygnatura z linkiem, temat, klasyfikacja, streszczenie.
  Streszczenie zajmuje pełną szerokość wiadomości.

  Mail idzie jako multipart/alternative — DWIE wersje tej samej treści:
    • text/plain — nagłówki sekcji WERSALIKAMI (fallback),
    • text/html  — nagłówki sekcji pogrubione, w kolorze marki.

  ŹRÓDŁO STRESZCZENIA
    Podstawą jest `streszczenie_pelne` — układ sekcyjny zapisywany przez
    GPT „Skaner Doradca — streszczanie zbiorcze” przez Edge Function
    wtyczka-zapisz. Sekcje: Podatek i temat, Opis stanu faktycznego, Pytania,
    Stanowisko podatnika, Stanowisko organu, Podstawy prawne.

    SEKCJA „PODSTAWY PRAWNE” JEST W MAILU POMIJANA — decyzja świadoma,
    wiadomość ma być czytana w skrzynce, a wykaz przepisów wydłuża ją bez
    korzyści. Pełny układ z przepisami zostaje dostępny w aplikacji i wtyczce.

    Gdy `streszczenie_pelne` jest puste (pole opcjonalne w schemacie
    wtyczka-zapisz — ok. 20% wpisów go nie ma), mail pokazuje zwięzłą prozę
    z kolumny `streszczenie` i zaznacza, że to wersja skrócona.

EGRESS
  Zapytania dobierają wyłącznie kolumny potrzebne do maila. Pełny tekst
  dokumentu (dokumenty.tekst) NIE jest pobierany — ILIKE liczy się po stronie
  bazy. Żadnych wywołań modelu: skrypt czyta gotowe streszczenia.

UWAGA — KLASYFIKACJA
  Wpisy z pustą kolumną `branze` albo `przedmiot` są NIEWIDOCZNE dla kanałów
  branżowego i przedmiotowego (złączenie ILIKE ich nie łapie). To nie jest
  usterka tego skryptu, tylko brak danych — patrz tryb `zakres` akcji
  doStreszczenia. Kanał fraz działa niezależnie od klasyfikacji.

Zmienne środowiskowe (GitHub Secrets):
  SUPABASE_DB_URL lub SUPABASE_HOST/USER/PASSWORD[/PORT/DB]
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM
Opcjonalne:
  MONITORING_OKNO_DNI — ile dni wstecz po pobrano_at (domyślnie 3)
  STRESZCZ_DATA_START — próg zakresu streszczania (domyślnie 2026-07-15)
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import db_core

OKNO_DNI = int(os.environ.get("MONITORING_OKNO_DNI") or "3")
# Próg zakresu streszczania. Interpretacje wydane wcześniej nie są streszczane,
# więc alertów fraz dla nich nie wstrzymujemy.
DATA_START_STRESZCZ = os.environ.get("STRESZCZ_DATA_START") or "2026-07-15"

# Kolory zaszyte na sztywno, BEZ importu paleta.py — ten skrypt chodzi
# w GitHub Actions bez Streamlita, a paleta.py go wymaga.
ZIELEN = "#386520"
TXT = "#111111"
TXT2 = "#555555"

# Nagłówki sekcji pomijane w mailu. Porównanie po zdjęciu znaczników markdown
# i sprowadzeniu do małych liter.
SEKCJE_POMIJANE = ("podstawy prawne",)


# ---------------------------------------------------------------------------
def _polacz() -> db_core.SupabaseDB:
    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return db_core.SupabaseDB({"url": url})
    braki = [k for k in ("SUPABASE_HOST", "SUPABASE_USER", "SUPABASE_PASSWORD")
             if not os.environ.get(k)]
    if braki:
        raise SystemExit("Brak konfiguracji bazy: " + ", ".join(braki))
    return db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT") or "5432",
        "database": os.environ.get("SUPABASE_DB") or "postgres",
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
        "sslmode": os.environ.get("SUPABASE_SSLMODE", "require"),
    })


def zapewnij_tabele(db: db_core.SupabaseDB) -> None:
    """Wywoływane też przez moduł Streamlit (wspólny schemat)."""
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS obserwowane_frazy (
            id        SERIAL PRIMARY KEY,
            fraza     TEXT NOT NULL,
            email     TEXT NOT NULL,
            podatek   TEXT DEFAULT '',      -- '' = wszystkie podatki
            aktywna   BOOLEAN DEFAULT TRUE,
            utworzono TEXT NOT NULL,
            UNIQUE (fraza, email, podatek)
        )
        """
    )
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS monitoring_wyslane (
            id          SERIAL PRIMARY KEY,
            fraza_id    INTEGER NOT NULL,
            dokument_id TEXT NOT NULL,
            wyslano     TEXT NOT NULL,
            UNIQUE (fraza_id, dokument_id)
        )
        """
    )
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS obserwowane_branze (
            id        SERIAL PRIMARY KEY,
            branza    TEXT NOT NULL,      -- wartość z taksonomii BRANZE
            email     TEXT NOT NULL,
            aktywna   BOOLEAN DEFAULT TRUE,
            utworzono TEXT NOT NULL,
            UNIQUE (branza, email)
        )
        """
    )
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS obserwowane_przedmioty (
            id        SERIAL PRIMARY KEY,
            przedmiot TEXT NOT NULL,      -- wartość z taksonomii PRZEDMIOTY
            podatek   TEXT NOT NULL,      -- podatek, z którego listy pochodzi
            email     TEXT NOT NULL,
            aktywna   BOOLEAN DEFAULT TRUE,
            utworzono TEXT NOT NULL,
            UNIQUE (przedmiot, email)
        )
        """
    )
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS monitoring_przedmioty_wyslane (
            id          SERIAL PRIMARY KEY,
            sub_id      INTEGER NOT NULL,
            dokument_id TEXT NOT NULL,
            wyslano     TEXT NOT NULL,
            UNIQUE (sub_id, dokument_id)
        )
        """
    )
    # Właściciel subskrypcji — metadana dla UI; skrypt wysyłkowy jej nie używa.
    for _tab in ("obserwowane_frazy", "obserwowane_branze", "obserwowane_przedmioty"):
        db.wykonaj(
            f"ALTER TABLE {_tab} ADD COLUMN IF NOT EXISTS wlasciciel TEXT DEFAULT ''")
        db.wykonaj(
            f"UPDATE {_tab} SET wlasciciel='DORADCA' "
            f"WHERE wlasciciel IS NULL OR wlasciciel=''")

    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS monitoring_branze_wyslane (
            id          SERIAL PRIMARY KEY,
            sub_id      INTEGER NOT NULL,
            dokument_id TEXT NOT NULL,
            wyslano     TEXT NOT NULL,
            UNIQUE (sub_id, dokument_id)
        )
        """
    )
    # Kolumny czytane przez maile — dokładane defensywnie, żeby skrypt nie padł
    # na bazie, w której moduł 6 jeszcze się nie uruchomił.
    db.wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS "
        "streszczenie_pelne TEXT DEFAULT ''")
    db.wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS "
        "zrodlo TEXT DEFAULT ''")


# ---------------------------------------------------------------------------
# ZAPYTANIA
# ---------------------------------------------------------------------------
def _trafienia(db: db_core.SupabaseDB) -> list[dict]:
    """Nowe pary (fraza, dokument): dopasowane, jeszcze nie wysłane."""
    return db.wykonaj(
        f"""
        SELECT f.id AS fraza_id, f.fraza, f.email, f.podatek AS fraza_podatek,
               d.id AS dokument_id, d.podatek, d.sygnatura, d.data_wyd,
               d.pobrano_at, d.link,
               s.temat, s.streszczenie,
               COALESCE(s.branze, '') AS branze,
               COALESCE(s.przedmiot, '') AS przedmiot,
               COALESCE(s.streszczenie_pelne, '') AS streszczenie_pelne
        FROM obserwowane_frazy f
        JOIN dokumenty d
          ON (f.podatek = '' OR f.podatek = d.podatek)
         AND (d.tekst ILIKE '%%' || f.fraza || '%%'
              OR d.sygnatura ILIKE '%%' || f.fraza || '%%')
        LEFT JOIN streszczenia_auto s ON s.dokument_id = d.id
        WHERE f.aktywna = TRUE
          AND d.pobrano_at >= now() - interval '{OKNO_DNI} days'
          AND NOT EXISTS (
                SELECT 1 FROM monitoring_wyslane w
                WHERE w.fraza_id = f.id AND w.dokument_id = d.id)
        ORDER BY f.email, f.fraza, d.data_wyd
        """,
        fetch=True,
    )


def _oznacz_wyslane(db: db_core.SupabaseDB, pary: list[dict]) -> None:
    teraz = dt.datetime.now().isoformat(timespec="seconds")
    for p in pary:
        db.wykonaj(
            """INSERT INTO monitoring_wyslane (fraza_id, dokument_id, wyslano)
               VALUES (%s,%s,%s) ON CONFLICT (fraza_id, dokument_id) DO NOTHING""",
            (p["fraza_id"], p["dokument_id"], teraz),
        )


def _trafienia_branz(db: db_core.SupabaseDB) -> list[dict]:
    """Nowe pary (subskrypcja branży, dokument): streszczenie z ostatnich
    OKNO_DNI dni ma przypisaną obserwowaną branżę, a powiadomienia jeszcze
    nie wysłano."""
    prog = (dt.datetime.now() - dt.timedelta(days=OKNO_DNI)).isoformat(
        timespec="seconds")
    return db.wykonaj(
        """
        SELECT b.id AS sub_id, b.branza, b.email,
               d.id AS dokument_id, d.podatek, d.sygnatura, d.data_wyd,
               d.pobrano_at, d.link,
               s.temat, s.streszczenie,
               COALESCE(s.branze, '') AS branze,
               COALESCE(s.przedmiot, '') AS przedmiot,
               COALESCE(s.streszczenie_pelne, '') AS streszczenie_pelne
        FROM obserwowane_branze b
        JOIN streszczenia_auto s
          ON s.branze ILIKE '%%' || b.branza || '%%'
        JOIN dokumenty d ON d.id = s.dokument_id
        WHERE b.aktywna = TRUE
          AND s.wygenerowano >= %s
          AND NOT EXISTS (
                SELECT 1 FROM monitoring_branze_wyslane w
                WHERE w.sub_id = b.id AND w.dokument_id = d.id)
        ORDER BY b.email, b.branza, d.data_wyd
        """,
        (prog,),
        fetch=True,
    )


def _oznacz_wyslane_branze(db: db_core.SupabaseDB, pary: list[dict]) -> None:
    teraz = dt.datetime.now().isoformat(timespec="seconds")
    for p in pary:
        db.wykonaj(
            """INSERT INTO monitoring_branze_wyslane (sub_id, dokument_id, wyslano)
               VALUES (%s,%s,%s) ON CONFLICT (sub_id, dokument_id) DO NOTHING""",
            (p["sub_id"], p["dokument_id"], teraz),
        )


def _trafienia_przedmiotow(db: db_core.SupabaseDB) -> list[dict]:
    """Nowe pary (subskrypcja przedmiotu, dokument).

    UWAGA: przedmiot SUBSKRYPCJI wychodzi jako `sub_przedmiot`, przedmiot
    przypisany DOKUMENTOWI jako `przedmiot`. We wcześniejszej wersji oba
    nazywały się tak samo i jeden nadpisywał drugi.
    """
    prog = (dt.datetime.now() - dt.timedelta(days=OKNO_DNI)).isoformat(
        timespec="seconds")
    return db.wykonaj(
        """
        SELECT p.id AS sub_id, p.przedmiot AS sub_przedmiot,
               p.podatek AS sub_podatek, p.email,
               d.id AS dokument_id, d.podatek, d.sygnatura, d.data_wyd,
               d.pobrano_at, d.link,
               s.temat, s.streszczenie,
               COALESCE(s.branze, '') AS branze,
               COALESCE(s.przedmiot, '') AS przedmiot,
               COALESCE(s.streszczenie_pelne, '') AS streszczenie_pelne
        FROM obserwowane_przedmioty p
        JOIN streszczenia_auto s
          ON s.przedmiot ILIKE '%%' || p.przedmiot || '%%'
        JOIN dokumenty d ON d.id = s.dokument_id
        WHERE p.aktywna = TRUE
          AND s.wygenerowano >= %s
          AND NOT EXISTS (
                SELECT 1 FROM monitoring_przedmioty_wyslane w
                WHERE w.sub_id = p.id AND w.dokument_id = d.id)
        ORDER BY p.email, p.przedmiot, d.data_wyd
        """,
        (prog,),
        fetch=True,
    )


def _oznacz_wyslane_przedmioty(db: db_core.SupabaseDB, pary: list[dict]) -> None:
    teraz = dt.datetime.now().isoformat(timespec="seconds")
    for p in pary:
        db.wykonaj(
            """INSERT INTO monitoring_przedmioty_wyslane (sub_id, dokument_id, wyslano)
               VALUES (%s,%s,%s) ON CONFLICT (sub_id, dokument_id) DO NOTHING""",
            (p["sub_id"], p["dokument_id"], teraz),
        )


# ---------------------------------------------------------------------------
# ROZBIÓR PEŁNEGO STRESZCZENIA NA SEKCJE
# ---------------------------------------------------------------------------
# Nagłówek sekcji rozpoznajemy DWOMA drogami, bo znaczniki markdown bywają
# gubione po drodze (zależnie od tego, czy GPT je wysłał i czy przetrwały
# zapis). Poleganie wyłącznie na gwiazdkach oznaczałoby, że przy wpisie bez
# nich nie rozpoznamy ANI JEDNEJ sekcji — całość poszłaby jednym blokiem,
# a sekcja pomijana by się nie wycięła.
#
#   1) linia w całości ujęta w ** ** (albo * *), opcjonalny dwukropek,
#   2) linia o treści zgodnej z nazwą znanej sekcji — niezależnie od gwiazdek.
_RE_NAGLOWEK_MD = re.compile(r"^\s*\*{1,2}\s*([^*]+?)\s*\*{1,2}\s*:?\s*$")
_RE_POGRUBIENIE = re.compile(r"\*\*(.+?)\*\*")

# Nazwy sekcji z instrukcji GPT „Skaner Doradca — streszczanie zbiorcze”.
# Porównanie po sprowadzeniu do małych liter i zdjęciu gwiazdek/dwukropka.
NAZWY_SEKCJI = (
    "podatek i temat",
    "opis stanu faktycznego",
    "pytania",
    "stanowisko podatnika",
    "stanowisko organu",
    "podstawy prawne",
)


def _jako_naglowek(linia: str) -> str | None:
    """Zwraca nazwę sekcji, jeśli linia jest jej nagłówkiem — inaczej None."""
    goly = linia.strip().strip("*").strip().rstrip(":").strip()
    if not goly or len(goly) > 60:
        return None
    if goly.lower() in NAZWY_SEKCJI:
        return goly
    m = _RE_NAGLOWEK_MD.match(linia)
    if m:
        return m.group(1).strip().rstrip(":").strip()
    return None


def _sekcje(pelne: str) -> list[tuple[str, list[str]]]:
    """Rozbija pełne streszczenie na [(nagłówek, linie treści)].

    Sekcje z SEKCJE_POMIJANE są odrzucane. Tekst przed pierwszym nagłówkiem
    (gdyby model go dokleił) trafia do sekcji o pustym nagłówku, żeby nic
    nie zginęło po cichu.
    """
    sekcje: list[tuple[str, list[str]]] = []
    stan = {"naglowek": "", "linie": []}

    def domknij() -> None:
        naglowek = stan["naglowek"]
        linie = [l for l in stan["linie"] if l.strip()]
        if naglowek or linie:
            if naglowek.strip().lower() not in SEKCJE_POMIJANE:
                sekcje.append((naglowek, linie))

    for linia in (pelne or "").split("\n"):
        nazwa = _jako_naglowek(linia)
        if nazwa:
            domknij()
            stan["naglowek"] = nazwa
            stan["linie"] = []
        else:
            stan["linie"].append(linia.rstrip())
    domknij()
    return sekcje


def _inline_html(tekst: str) -> str:
    """Escape + zamiana **pogrubień** na <strong>. Kolejność jest istotna:
    najpierw escape (żeby treść nie wstrzyknęła HTML-a), potem znaczniki."""
    return _RE_POGRUBIENIE.sub(r"<strong>\1</strong>", html.escape(tekst))


def _inline_tekst(tekst: str) -> str:
    """Wersja tekstowa: gwiazdki pogrubienia po prostu znikają."""
    return _RE_POGRUBIENIE.sub(r"\1", tekst)


def _fmt_data(iso) -> str:
    s = str(iso or "")[:10]
    if not s:
        return "—"
    try:
        return dt.date.fromisoformat(s).strftime("%d.%m.%Y")
    except Exception:
        return s


# ---------------------------------------------------------------------------
# BLOK JEDNEJ INTERPRETACJI — HTML
# ---------------------------------------------------------------------------
_STYL_NAGL_SEKCJI = (f"margin:10px 0 3px 0;font-size:12px;font-weight:700;"
                     f"color:{ZIELEN};text-transform:uppercase;"
                     f"letter-spacing:.4px;")
_STYL_AKAPIT = "margin:0 0 5px 0;font-size:14px;line-height:1.55;"


def _blok_html(t: dict) -> str:
    sygn = html.escape((t.get("sygnatura") or "").strip())
    link = (t.get("link") or "").strip()
    naglowek_sygn = (
        f"<a href='{html.escape(link, quote=True)}' "
        f"style='color:{ZIELEN};text-decoration:none;'>{sygn}</a>"
        if link else sygn
    )

    czesci = [
        f"<p style='margin:0 0 2px 0;font-size:14px;'>"
        f"<strong>[{html.escape(str(t.get('podatek') or ''))}]</strong> "
        f"{naglowek_sygn}"
        f"<span style='color:{TXT2};font-size:12px;'> · wydana "
        f"{_fmt_data(t.get('data_wyd'))} · opublikowana "
        f"{_fmt_data(t.get('pobrano_at'))}</span></p>"
    ]

    temat = (t.get("temat") or "").strip()
    if temat:
        czesci.append(
            f"<p style='margin:0 0 2px 0;font-size:14px;'>"
            f"<strong>Temat:</strong> {html.escape(temat)}</p>")

    kl = []
    if (t.get("branze") or "").strip():
        kl.append(f"branża: {t['branze'].strip()}")
    if (t.get("przedmiot") or "").strip():
        kl.append(f"przedmiot: {t['przedmiot'].strip()}")
    if kl:
        czesci.append(
            f"<p style='margin:0 0 8px 0;font-size:12px;color:{TXT2};'>"
            f"({html.escape('; '.join(kl))})</p>")

    pelne = (t.get("streszczenie_pelne") or "").strip()
    sekcje = _sekcje(pelne) if pelne else []

    if sekcje:
        for naglowek, linie in sekcje:
            if naglowek:
                czesci.append(f"<p style='{_STYL_NAGL_SEKCJI}'>"
                              f"{html.escape(naglowek)}</p>")
            for l in linie:
                czesci.append(f"<p style='{_STYL_AKAPIT}'>"
                              f"{_inline_html(l.strip())}</p>")
    else:
        proza = (t.get("streszczenie") or "").strip()
        if proza:
            czesci.append(f"<p style='{_STYL_NAGL_SEKCJI}'>"
                          f"Streszczenie (wersja skrócona)</p>")
            czesci.append(f"<p style='{_STYL_AKAPIT}'>{html.escape(proza)}</p>")
        else:
            czesci.append(
                f"<p style='margin:10px 0 5px 0;font-size:13px;color:{TXT2};'>"
                f"Streszczenie jeszcze niegotowe.</p>")

    return (f"<div style='margin:0 0 22px 0;padding:0 0 0 14px;"
            f"border-left:3px solid {ZIELEN};'>" + "".join(czesci) + "</div>")


def _html_maila(trafienia: list[dict], klucz: str, tytul: str,
                etykieta_grupy: str, stopka: str) -> str:
    wg: dict[str, list[dict]] = {}
    for t in trafienia:
        wg.setdefault(t[klucz], []).append(t)

    sekcje = []
    for nazwa, lista in wg.items():
        sekcje.append(
            f"<h3 style='color:{ZIELEN};font-size:15px;margin:26px 0 10px 0;"
            f"padding-bottom:4px;border-bottom:2px solid {ZIELEN};'>"
            f"{etykieta_grupy}: {html.escape(str(nazwa))} — trafień: {len(lista)}"
            f"</h3>" + "".join(_blok_html(t) for t in lista)
        )

    return (
        f"<html><body style='font-family:Arial,Helvetica,sans-serif;"
        f"color:{TXT};margin:0;padding:16px;max-width:820px;'>"
        f"<h2 style='color:{ZIELEN};font-size:18px;margin:0 0 2px 0;'>{tytul}</h2>"
        f"<p style='color:{TXT2};font-size:12px;margin:0;'>"
        f"Skaner Doradca — powiadomienie automatyczne</p>"
        + "".join(sekcje) +
        f"<p style='color:{TXT2};font-size:11px;margin-top:26px;'>{stopka}</p>"
        f"</body></html>"
    )


# ---------------------------------------------------------------------------
# BLOK JEDNEJ INTERPRETACJI — TEKST (fallback multipart/alternative)
# ---------------------------------------------------------------------------
def _blok_streszczenia(t: dict) -> list[str]:
    linie = []
    if t.get("temat"):
        linie.append(f"     Temat: {t['temat']}")
    kl = []
    if (t.get("branze") or "").strip():
        kl.append(f"branża: {t['branze'].strip()}")
    if (t.get("przedmiot") or "").strip():
        kl.append(f"przedmiot: {t['przedmiot'].strip()}")
    if kl:
        linie.append("     (" + "; ".join(kl) + ")")

    pelne = (t.get("streszczenie_pelne") or "").strip()
    sekcje = _sekcje(pelne) if pelne else []
    if sekcje:
        for naglowek, tresc in sekcje:
            linie.append("")
            if naglowek:
                linie.append(f"     {naglowek.upper()}")
            for l in tresc:
                linie.append(f"       {_inline_tekst(l).strip()}")
    else:
        proza = (t.get("streszczenie") or "").strip()
        if proza:
            linie.append("     STRESZCZENIE (WERSJA SKRÓCONA)")
            linie.append(f"       {proza}")
        else:
            linie.append("     Streszczenie jeszcze niegotowe.")
    return linie


def _naglowek_pozycji(t: dict) -> list[str]:
    linie = [f"   • [{t['podatek']}] {t['sygnatura']} "
             f"(wydana {str(t['data_wyd'])[:10]})"]
    if t.get("link"):
        linie.append(f"     {t['link']}")
    return linie


def _tekst_maila(trafienia: list[dict], klucz: str, tytul: str,
                 etykieta_grupy: str, stopka: str) -> str:
    linie = [tytul, "(Skaner Doradca — powiadomienie automatyczne)", ""]
    wg: dict[str, list[dict]] = {}
    for t in trafienia:
        wg.setdefault(t[klucz], []).append(t)
    for nazwa, lista in wg.items():
        linie.append(f"■ {etykieta_grupy}: {nazwa} — trafień: {len(lista)}")
        for t in lista:
            linie.extend(_naglowek_pozycji(t))
            linie.extend(_blok_streszczenia(t))
            linie.append("")
        linie.append("")
    linie.append("— " + stopka)
    return "\n".join(linie)


# ---------------------------------------------------------------------------
# WYSYŁKA
# ---------------------------------------------------------------------------
def _wyslij_temat(adres: str, tresc: str, temat: str,
                  tresc_html: str | None = None) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER")
    haslo = os.environ.get("SMTP_PASSWORD")
    if not (host and user and haslo):
        raise SystemExit("Brak konfiguracji SMTP (SMTP_HOST/SMTP_USER/SMTP_PASSWORD).")
    nadawca = os.environ.get("SMTP_FROM") or user

    if tresc_html:
        msg = MIMEMultipart("alternative")
        # Kolejność ma znaczenie: wersja preferowana idzie OSTATNIA.
        msg.attach(MIMEText(tresc, "plain", "utf-8"))
        msg.attach(MIMEText(tresc_html, "html", "utf-8"))
    else:
        msg = MIMEText(tresc, "plain", "utf-8")

    msg["Subject"] = temat
    msg["From"] = formataddr(("Skaner Doradca", nadawca))
    msg["To"] = adres

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, haslo)
        s.sendmail(nadawca, [adres], msg.as_string())


# ---------------------------------------------------------------------------
# OBSŁUGA JEDNEGO KANAŁU
# ---------------------------------------------------------------------------
def _obsluz_kanal(db, trafienia: list[dict], klucz: str, tytul: str,
                  etykieta: str, temat_wzor: str, stopka: str,
                  oznacz, nazwa_logu: str) -> None:
    """Grupowanie per adres, wysyłka i oznaczenie. Wspólne dla trzech kanałów —
    wcześniej ten sam blok był powielony trzy razy."""
    wg_adresu: dict[str, list[dict]] = {}
    for t in trafienia:
        wg_adresu.setdefault(t["email"].strip(), []).append(t)
    for adres, lista in wg_adresu.items():
        try:
            _wyslij_temat(
                adres,
                _tekst_maila(lista, klucz, tytul, etykieta, stopka),
                temat_wzor.format(ile=len(lista)),
                _html_maila(lista, klucz, tytul, etykieta, stopka),
            )
            oznacz(db, lista)
            print(f"[monitoring] {nazwa_logu} → {adres}: {len(lista)} trafień.")
        except SystemExit:
            raise
        except Exception as e:
            print(f"[monitoring] BŁĄD wysyłki ({nazwa_logu}) na {adres}: {e}")


# ---------------------------------------------------------------------------
def main() -> int:
    db = _polacz()
    zapewnij_tabele(db)

    # ── kanał 1: frazy ──────────────────────────────────────────────────────
    trafienia = _trafienia(db)
    # Wstrzymanie: interpretacja w zakresie streszczania (data_wyd >= progu)
    # bez streszczenia NIE jest wysyłana ani oznaczana — doślemy ją, gdy
    # streszczenie powstanie. Interpretacje spoza zakresu idą od razu.
    gotowe, wstrzymane = [], 0
    for t in trafienia:
        ma_streszcz = bool((t.get("streszczenie") or "").strip())
        w_zakresie = str(t.get("data_wyd") or "") >= DATA_START_STRESZCZ
        if not ma_streszcz and w_zakresie:
            wstrzymane += 1
            continue
        gotowe.append(t)
    trafienia = gotowe
    print(f"[monitoring] Frazy | okno: {OKNO_DNI} dni | do wysłania: {len(trafienia)}"
          + (f" | wstrzymano do streszczenia: {wstrzymane}" if wstrzymane else ""))
    _obsluz_kanal(
        db, trafienia, "fraza",
        "Nowe interpretacje pasujące do obserwowanych fraz", "Fraza",
        "[Skaner Doradca] Monitoring fraz: {ile} nowych trafień",
        "Wiadomość wygenerowana automatycznie. Frazy zarządzasz w aplikacji, "
        "moduł „Monitoring Fraz”.",
        _oznacz_wyslane, "Frazy")

    # ── kanał 2: branże ─────────────────────────────────────────────────────
    tr_b = _trafienia_branz(db)
    print(f"[monitoring] Branże | nowych trafień: {len(tr_b)}")
    _obsluz_kanal(
        db, tr_b, "branza",
        "Nowe interpretacje z obserwowanych branż", "Branża",
        "[Skaner Doradca] Monitoring branż: {ile} nowych trafień",
        "Wiadomość wygenerowana automatycznie. Branże zarządzasz w aplikacji, "
        "moduł „Monitoring Branż”.",
        _oznacz_wyslane_branze, "Branże")

    # ── kanał 3: przedmioty ─────────────────────────────────────────────────
    tr_p = _trafienia_przedmiotow(db)
    print(f"[monitoring] Przedmioty | nowych trafień: {len(tr_p)}")
    _obsluz_kanal(
        db, tr_p, "sub_przedmiot",
        "Nowe interpretacje z obserwowanych obszarów", "Przedmiot",
        "[Skaner Doradca] Monitoring przedmiotów: {ile} nowych trafień",
        "Wiadomość wygenerowana automatycznie. Obszary zarządzasz w aplikacji, "
        "moduł „Monitoring Przedmiotów”.",
        _oznacz_wyslane_przedmioty, "Przedmioty")

    if not trafienia and not tr_b and not tr_p:
        print("[monitoring] Nic do wysłania.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
