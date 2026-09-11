# -*- coding: utf-8 -*-
"""
alarm_awarii.py — ALARM O CICHEJ AWARII (GitHub Actions, NIEZALEŻNY workflow).

Pilnuje, czy codzienna synchronizacja interpretacji naprawdę działa. Ponieważ
streszczanie i monitoring są uruchamiane łańcuchowo PO synchronizacji, zdrowa
synchronizacja jest sercem całego potoku — jeśli ona milczy, milczy wszystko.

Dwa wykrywane stany awaryjne (z historia_synchronizacji):
  • CISZA  — brak wpisu świeższego niż PROG_GODZIN (synchronizacja się nie
             uruchamia: workflow wyłączony, blokada MF, awaria Actions…).
  • BŁĄD   — ostatni przebieg zapisał status ERROR dla któregoś podatku.

Alarm jest wysyłany e-mailem i NIE spamuje: wysyła się raz przy przejściu
w stan awarii, potem najwyżej raz na COOLDOWN_GODZIN, a gdy wróci do normy —
wysyła jedno powiadomienie „przywrócono”. Stan trzymany w tabeli alarm_stan.

Ten workflow jest CELOWO poza łańcuchem — działa własnym harmonogramem, żeby
zadziałał także wtedy, gdy łańcuch jest zerwany.

Zmienne środowiskowe:
  SUPABASE_DB_URL lub SUPABASE_HOST/USER/PASSWORD[/PORT/DB]
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM
  ALARM_ODBIORCA        — adres alarmów (domyślnie EMAIL_ODBIORCA lub SMTP_USER)
  ALARM_PROG_GODZIN     — po ilu h bez wpisu to „cisza” (domyślnie 14)
  ALARM_COOLDOWN_GODZIN — min. odstęp między przypomnieniami (domyślnie 24)
"""

from __future__ import annotations

import datetime as dt
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.utils import formataddr

import db_core

PROG_GODZIN = float(os.environ.get("ALARM_PROG_GODZIN") or "14")
COOLDOWN_GODZIN = float(os.environ.get("ALARM_COOLDOWN_GODZIN") or "24")


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


def _zapewnij_tabele(db) -> None:
    db.wykonaj(
        """
        CREATE TABLE IF NOT EXISTS alarm_stan (
            klucz           TEXT PRIMARY KEY,   -- np. 'synchronizacja'
            stan            TEXT NOT NULL,      -- OK / CISZA / BLAD
            ostatnia_zmiana TEXT DEFAULT '',
            ostatni_alert   TEXT DEFAULT ''
        )
        """
    )


# ---------------------------------------------------------------------------
def _parsuj_ts(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None


def _ocena_synchronizacji(db) -> tuple[str, str]:
    """Zwraca (stan, opis). Stan: OK | CISZA | BLAD."""
    rows = db.wykonaj(
        """SELECT uruchomiono, podatek, status, nowych_dok, szczegoly
           FROM historia_synchronizacji
           ORDER BY uruchomiono DESC LIMIT 20""",
        fetch=True,
    )
    if not rows:
        return "CISZA", "W historii synchronizacji nie ma żadnego wpisu."

    ostatni_ts = rows[0]["uruchomiono"]
    ts = _parsuj_ts(ostatni_ts)
    if ts:
        wiek_h = (dt.datetime.now() - ts).total_seconds() / 3600
        if wiek_h > PROG_GODZIN:
            return ("CISZA",
                    f"Ostatnia synchronizacja: {str(ostatni_ts)[:16].replace('T', ' ')} "
                    f"(ok. {wiek_h:.0f} h temu). Harmonogram przewiduje przebiegi "
                    f"kilka razy dziennie — brak świeższego wpisu oznacza, że "
                    f"synchronizacja się nie uruchamia (sprawdź: czy workflow "
                    f"włączony, czy MF nie blokuje, czy Actions działa).")

    # Ten sam przebieg = wiersze o tym samym znaczniku uruchomiono.
    biezacy = [r for r in rows if r["uruchomiono"] == ostatni_ts]
    bledne = [r for r in biezacy if (r.get("status") or "").upper() == "ERROR"]
    if bledne:
        podatki = ", ".join(sorted({r["podatek"] for r in bledne}))
        szczeg = "; ".join(sorted({(r.get("szczegoly") or "").strip()
                                   for r in bledne if r.get("szczegoly")}))
        return ("BLAD",
                f"Ostatni przebieg ({str(ostatni_ts)[:16].replace('T', ' ')}) "
                f"zakończył się błędem dla: {podatki}."
                + (f" Szczegóły: {szczeg}" if szczeg else ""))

    nowe = sum(int(r.get("nowych_dok") or 0) for r in biezacy)
    return ("OK",
            f"Ostatnia synchronizacja: {str(ostatni_ts)[:16].replace('T', ' ')} "
            f"(nowych interpretacji: {nowe}).")


# ---------------------------------------------------------------------------
def _wyslij(temat: str, tresc: str) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER")
    haslo = os.environ.get("SMTP_PASSWORD")
    if not (host and user and haslo):
        raise SystemExit("Brak konfiguracji SMTP.")
    nadawca = os.environ.get("SMTP_FROM") or user
    odbiorca = (os.environ.get("ALARM_ODBIORCA")
                or os.environ.get("EMAIL_ODBIORCA") or user)

    msg = MIMEText(tresc, "plain", "utf-8")
    msg["Subject"] = temat
    msg["From"] = formataddr(("Skaner Doradca — Alarm", nadawca))
    msg["To"] = odbiorca
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, haslo)
        s.sendmail(nadawca, [odbiorca], msg.as_string())
    print(f"[alarm] Wysłano e-mail: {temat} -> {odbiorca}")


def _stan_zapisany(db, klucz: str) -> dict | None:
    r = db.wykonaj("SELECT * FROM alarm_stan WHERE klucz=%s", (klucz,), fetch=True)
    return r[0] if r else None


def _zapisz_stan(db, klucz: str, stan: str, alert_teraz: bool,
                 poprzedni: dict | None) -> None:
    teraz = dt.datetime.now().isoformat(timespec="seconds")
    ostatni_alert = (teraz if alert_teraz
                     else (poprzedni or {}).get("ostatni_alert", ""))
    db.wykonaj(
        """INSERT INTO alarm_stan (klucz, stan, ostatnia_zmiana, ostatni_alert)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (klucz) DO UPDATE SET
               stan=EXCLUDED.stan, ostatnia_zmiana=EXCLUDED.ostatnia_zmiana,
               ostatni_alert=EXCLUDED.ostatni_alert""",
        (klucz, stan, teraz, ostatni_alert),
    )


# ---------------------------------------------------------------------------
def main() -> int:
    db = _polacz()
    _zapewnij_tabele(db)

    klucz = "synchronizacja"
    stan, opis = _ocena_synchronizacji(db)
    poprzedni = _stan_zapisany(db, klucz)
    stan_prev = (poprzedni or {}).get("stan", "OK")
    print(f"[alarm] Stan: {stan} (poprzednio: {stan_prev}) | {opis}")

    awaria = stan in ("CISZA", "BLAD")
    alert_teraz = False

    if awaria:
        # Alarmuj przy wejściu w awarię albo po upływie cooldownu.
        wysylac = stan_prev == "OK"
        if not wysylac:
            ost = _parsuj_ts((poprzedni or {}).get("ostatni_alert", "") or "")
            if not ost or (dt.datetime.now() - ost).total_seconds() / 3600 >= COOLDOWN_GODZIN:
                wysylac = True
        if wysylac:
            tytul = ("cisza — synchronizacja nie działa" if stan == "CISZA"
                     else "błąd synchronizacji")
            _wyslij(
                f"[Skaner Doradca] ALARM: {tytul}",
                "Wykryto problem z automatyczną synchronizacją interpretacji.\n\n"
                f"{opis}\n\n"
                "To wiadomość z automatycznego nadzoru (alarm o cichej awarii). "
                "Kolejne przypomnienie najwcześniej za "
                f"{int(COOLDOWN_GODZIN)} h, o ile problem nie ustąpi. Gdy "
                "synchronizacja wróci do normy, dostaniesz powiadomienie "
                "o przywróceniu.\n\n— Skaner Doradca",
            )
            alert_teraz = True
    else:
        # Powrót do normy po wcześniejszej awarii — jedno powiadomienie.
        if stan_prev in ("CISZA", "BLAD"):
            _wyslij(
                "[Skaner Doradca] Przywrócono — synchronizacja znów działa",
                f"Synchronizacja interpretacji wróciła do normy.\n\n{opis}\n\n"
                "— Skaner Doradca",
            )
            alert_teraz = True

    _zapisz_stan(db, klucz, stan, alert_teraz, poprzedni)
    return 0


if __name__ == "__main__":
    sys.exit(main())
