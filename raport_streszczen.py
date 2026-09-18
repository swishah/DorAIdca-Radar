# -*- coding: utf-8 -*-
"""
raport_streszczen.py — dzienny mail o streszczeniach.

PO CO
    Streszczenia pisze kilku autorów naraz: automat Gemini w chmurze, automat
    OpenRoutera (PCC i CUKIER), Custom GPT, projekt Claude i wtyczka u doradcy.
    Panel pokazuje to na stronie „Harmonogram streszczania", ale nikt tam nie
    zagląda codziennie. Ten mail przychodzi sam i mówi, czy automat w nocy
    pracował, czy stanął.

    Odpowiednik maila po synchronizacji dziennej — ten mówi, co POBRANO,
    a ten, co STRESZCZONO.

SKĄD DANE
    Z bazy w chmurze, bo tam pisze automat. Docker dostaje te wpisy dopiero
    synchronizacją o 7:30, więc raport liczony z Dockera pokazywałby wczoraj.

KIEDY ALARMUJE
    Temat maila zaczyna się od ❌, gdy przez dobę nie powstało ANI JEDNO
    streszczenie, a kolejka nie jest pusta — to znaczy, że automat stanął
    i nikt się o tym nie dowie inaczej.

URUCHOMIENIE
    python raport_streszczen.py            # wysyła
    python raport_streszczen.py --sucho    # wypisuje na ekran, nie wysyła
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db_core

# Klucz modelu w streszczenia_auto — ten sam, o który pyta kolejka. Po nim
# rozpoznaje się wiersz, nie autora; autor siedzi w kolumnie `zrodlo`.
MODEL_KANONICZNY = os.environ.get("MODEL_ZESTAWIENIA", "").strip() or "openrouter/free"
DATA_START = os.environ.get("STRESZCZ_DATA_START", "").strip() or "2026-07-15"
MAKS_W_TABELI = 40


def _autor(zrodlo: str) -> str:
    """Czytelna nazwa autora z kolumny `zrodlo`. Te same reguły co w panelu
    (ui/app.py, _rozbierz_zrodlo) — zmiana tam to zmiana tutaj."""
    z = (zrodlo or "").strip()
    if not z:
        return "nieznane"
    przedrostek, _, szczegol = z.partition(":")
    if przedrostek == "wtyczka":
        return "Wtyczka"
    if przedrostek == "gemini":
        return "Gemini (Docker)"
    if przedrostek == "gpt":
        if szczegol.startswith("gemini"):
            return "Gemini"
        if szczegol.startswith("deepseek") or szczegol.startswith("nex") or szczegol.startswith("dots"):
            return "OpenRouter"
        if szczegol == "claude":
            return "Claude"
        return "ChatGPT"
    return z


def zbierz(db) -> dict:
    ostatnie = db.wykonaj(
        """SELECT d.sygnatura, d.podatek, s.zrodlo, s.temat, s.wygenerowano,
                  length(coalesce(s.streszczenie, '')) AS dl_krotkie,
                  length(coalesce(s.streszczenie_pelne, '')) AS dl_pelne
           FROM streszczenia_auto s JOIN dokumenty d ON d.id = s.dokument_id
           WHERE s.model = %s AND coalesce(s.streszczenie, '') <> ''
             AND s.wygenerowano::timestamptz >= now() - interval '24 hours'
           ORDER BY s.wygenerowano DESC""", (MODEL_KANONICZNY,), fetch=True) or []

    kolejka = db.wykonaj(
        """SELECT d.podatek, count(*) AS ile
           FROM dokumenty d
           LEFT JOIN streszczenia_auto s
                  ON s.dokument_id = d.id AND s.model = %s
           WHERE d.data_wyd >= %s
             AND (s.dokument_id IS NULL OR coalesce(s.streszczenie, '') = ''
                  OR length(s.streszczenie) < 120)
           GROUP BY 1 ORDER BY 2 DESC""",
        (MODEL_KANONICZNY, DATA_START), fetch=True) or []

    # Dokumenty odłożone przez licznik nieudanych prób — jeżeli ich przybywa,
    # coś jest nie tak z konkretnymi dokumentami, nie z automatem.
    try:
        odlozone = db.wykonaj(
            """SELECT d.sygnatura, n.prob, n.ostatni_blad
               FROM streszczanie_niepowodzenia n
               JOIN dokumenty d ON d.id = n.dokument_id
               WHERE n.prob >= 3 ORDER BY n.prob DESC LIMIT 10""", fetch=True) or []
    except Exception:
        odlozone = []          # tabela istnieje tylko w chmurze

    autorzy = {}
    for w in ostatnie:
        autorzy[_autor(w["zrodlo"])] = autorzy.get(_autor(w["zrodlo"]), 0) + 1
    return {"ostatnie": ostatnie, "kolejka": kolejka, "odlozone": odlozone,
            "autorzy": sorted(autorzy.items(), key=lambda x: -x[1])}


def zbuduj(dane: dict) -> tuple:
    ile = len(dane["ostatnie"])
    czeka = sum(k["ile"] for k in dane["kolejka"])
    stoi = ile == 0 and czeka > 0
    znacznik = "❌" if stoi else ("✅" if ile else "💤")
    temat = f"{znacznik} DorAIdca Radar — streszczenia z ostatniej doby ({ile})"

    autorzy = "".join(f"<tr><td>{a}</td><td align='right'>{n}</td></tr>"
                      for a, n in dane["autorzy"]) or "<tr><td colspan='2'>—</td></tr>"
    kolejka = "".join(f"<tr><td>{k['podatek']}</td><td align='right'>{k['ile']}</td></tr>"
                      for k in dane["kolejka"]) or "<tr><td colspan='2'>pusto</td></tr>"
    wiersze = "".join(
        f"<tr><td>{str(w['wygenerowano'])[11:16]}</td><td>{w['sygnatura']}</td>"
        f"<td>{w['podatek']}</td><td>{_autor(w['zrodlo'])}</td>"
        f"<td>{(w['temat'] or '')[:90]}</td>"
        f"<td align='right'>{w['dl_krotkie']}/{w['dl_pelne']}</td></tr>"
        for w in dane["ostatnie"][:MAKS_W_TABELI]) or \
        "<tr><td colspan='6'>Przez ostatnią dobę nie powstało żadne streszczenie.</td></tr>"

    alarm = ""
    if stoi:
        alarm = ("<p style='background:#fdecea;padding:10px;border-left:4px solid #b83a3a'>"
                 f"<b>Automat prawdopodobnie stoi.</b> Przez dobę nie powstało żadne "
                 f"streszczenie, a w kolejce czeka {czeka}. Sprawdź stronę "
                 "&bdquo;Harmonogram streszczania&rdquo; i przebiegi pg_cron w Supabase.</p>")
    odlozone = ""
    if dane["odlozone"]:
        pozycje = "".join(f"<li>{o['sygnatura']} — {o['prob']} prób: {o['ostatni_blad'][:120]}</li>"
                          for o in dane["odlozone"])
        odlozone = f"<h3>Odłożone po nieudanych próbach</h3><ul>{pozycje}</ul>"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; font-size: 14px;">
    <h2>📝 DorAIdca Radar — streszczenia z ostatniej doby</h2>
    {alarm}
    <p><b>Powstało:</b> {ile} · <b>w kolejce:</b> {czeka}</p>

    <h3>Kto streszczał</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
      <tr style="background:#eee"><th align="left">Autor</th><th>Ile</th></tr>{autorzy}
    </table>

    <h3>Kolejka</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
      <tr style="background:#eee"><th align="left">Podatek</th><th>Czeka</th></tr>{kolejka}
    </table>

    {odlozone}

    <h3>Streszczenia (najnowsze {min(ile, MAKS_W_TABELI)})</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; font-size:12px">
      <tr style="background:#eee">
        <th>Godz.</th><th align="left">Sygnatura</th><th>Podatek</th><th align="left">Autor</th>
        <th align="left">Temat</th><th>Znaków</th>
      </tr>{wiersze}
    </table>

    <p style="color:#666; font-size:12px; margin-top:18px">
      Liczby z bazy w chmurze — tam pisze automat. Do panelu wpisy schodzą
      synchronizacją o 7:30 i 0:30.
    </p>
    </body></html>"""
    return temat, html


def wyslij(temat: str, html: str) -> bool:
    adres = os.environ.get("GMAIL_ADRES")
    haslo = os.environ.get("GMAIL_HASLO_APLIKACJI")
    odbiorca = os.environ.get("EMAIL_ODBIORCA", adres)
    if not adres or not haslo:
        print("Brak konfiguracji poczty — nie wysyłam.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = temat, adres, odbiorca
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(adres, haslo)
            s.send_message(msg)
        print(f"Wysłano do {odbiorca}: {temat}")
        return True
    except Exception as e:
        print(f"BŁĄD wysyłki: {e}")
        return False


def main() -> int:
    sucho = "--sucho" in sys.argv
    db = db_core.SupabaseDB({
        "host": os.environ["SUPABASE_HOST"],
        "port": os.environ.get("SUPABASE_PORT", "5432"),
        "database": os.environ.get("SUPABASE_DB", "postgres"),
        "user": os.environ["SUPABASE_USER"],
        "password": os.environ["SUPABASE_PASSWORD"],
    })
    temat, html = zbuduj(zbierz(db))
    print(temat)
    if sucho:
        print(html[:1500])
        return 0
    # Nieudana wysyłka kładzie przebieg — cicha awaria poczty jest gorsza od
    # braku maila, bo nikt nie wie, że raport przestał przychodzić.
    return 0 if wyslij(temat, html) else 1


if __name__ == "__main__":
    raise SystemExit(main())
