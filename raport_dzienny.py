# -*- coding: utf-8 -*-
"""
raport_dzienny.py — jeden dzienny mail: co POBRANO i co STRESZCZONO.

PO CO JEDEN ZAMIAST DWÓCH
    Do 21.09.2026 przychodziły dwa listy: jeden po synchronizacji dziennej
    (co pobrano z MF) i drugi o streszczeniach. Mówiły o dwóch połowach tej
    samej doby i czytało się je razem albo wcale. Życzenie właściciela:
    „jeden mail z jednym i drugim wystarczy".

    Skutek uboczny jest korzystny: mail wychodzi TERAZ także wtedy, gdy
    synchronizacja w ogóle nie doszła do końca. Poprzednio wysyłał go sam
    przebieg synchronizacji, więc gdy ginął na limicie czasu (tak stało się
    19 i 20.09.2026, gdy MF miało prace serwisowe), nie przychodziło nic —
    cisza nie do odróżnienia od spokojnego dnia.

SKĄD DANE
    Wszystko z bazy w chmurze: tam pisze i synchronizacja (dokumenty,
    historia_synchronizacji), i automat streszczający (streszczenia_auto).
    Docker dostaje te wpisy dopiero synchronizacją o 7:30, więc raport
    liczony z Dockera pokazywałby wczoraj.

KIEDY ALARMUJE (❌ w temacie)
    - przez dobę nie powstało ANI JEDNO streszczenie, a kolejka nie jest pusta,
    - albo ostatnia udana synchronizacja jest starsza niż DOBA_ALARMU godzin.

URUCHOMIENIE
    python raport_dzienny.py            # wysyła
    python raport_dzienny.py --sucho    # wypisuje na ekran, nie wysyła
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

# Synchronizacja chodzi trzy razy na dobę. Próg z zapasem: jeden nieudany
# przebieg nie alarmuje, brak udanego przez ponad dobę — owszem.
DOBA_ALARMU = 26


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


# ---------------------------------------------------------------------------
# DANE
# ---------------------------------------------------------------------------
def zbierz_synchronizacje(db) -> dict:
    """Co pobrano z MF przez ostatnią dobę i kiedy ostatnio się udało.

    `uruchomiono` i `pobrano_dt` są w bazie TEKSTEM w formacie ISO, więc
    porównujemy tekstowo — daje ten sam porządek i nie wymaga rzutowania
    każdego wiersza."""
    przebiegi = db.wykonaj(
        """SELECT uruchomiono, podatek, liczba_dok, nowych_dok, status,
                  coalesce(szczegoly, '') AS szczegoly
           FROM historia_synchronizacji
           WHERE uruchomiono >= to_char(now() - interval '24 hours', 'YYYY-MM-DD"T"HH24:MI:SS')
           ORDER BY uruchomiono DESC, podatek""", fetch=True) or []

    ostatni_ok = db.wykonaj(
        "SELECT max(uruchomiono) AS kiedy FROM historia_synchronizacji WHERE status = 'OK'",
        fetch=True) or [{}]
    ostatni_ok = (ostatni_ok[0] or {}).get("kiedy")

    godzin = db.wykonaj(
        """SELECT round(extract(epoch FROM now() - coalesce(max(uruchomiono::timestamp),
                                                            now() - interval '999 days')) / 3600)::int AS h
           FROM historia_synchronizacji WHERE status = 'OK'""", fetch=True) or [{}]
    godzin = (godzin[0] or {}).get("h") or 0

    # Dokumenty, ktore FAKTYCZNIE przybyly — liczone po dokumentach, nie po
    # deklaracji przebiegu: przebieg potrafi skonczyc sie OK i nie przyniesc nic.
    nowe = db.wykonaj(
        """SELECT podatek, count(*) AS ile
           FROM dokumenty
           WHERE pobrano_at >= now() - interval '24 hours'
           GROUP BY 1 ORDER BY 2 DESC""", fetch=True) or []

    # Braki w archiwum wedlug audytu kompletnosci (schemat 24). Do 21.09.2026
    # nie dalo sie odpowiedziec na pytanie „czy archiwum jest kompletne";
    # teraz odpowiedz przychodzi sama, codziennie.
    try:
        braki = db.wykonaj(
            """SELECT podatek, sum(brakuje)::int AS brakuje, count(*)::int AS miesiecy
               FROM braki_archiwum GROUP BY 1 ORDER BY 2 DESC""", fetch=True) or []
    except Exception:
        braki = []              # tabela istnieje tylko w chmurze

    return {"przebiegi": przebiegi, "ostatni_ok": ostatni_ok,
            "godzin_od_ok": godzin, "nowe": nowe, "braki": braki,
            "brakuje_razem": sum(b["brakuje"] for b in braki),
            "nowych_razem": sum(n["ile"] for n in nowe)}


def zbierz_streszczenia(db) -> dict:
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


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
def zbuduj(sync: dict, stre: dict) -> tuple:
    ile = len(stre["ostatnie"])
    czeka = sum(k["ile"] for k in stre["kolejka"])
    nowych = sync["nowych_razem"]

    stoi_streszczanie = ile == 0 and czeka > 0
    stoi_pobieranie = sync["godzin_od_ok"] > DOBA_ALARMU
    znacznik = "❌" if (stoi_streszczanie or stoi_pobieranie) else ("✅" if (ile or nowych) else "💤")
    temat = "%s DorAIdca Radar — %d nowych, %d streszczeń" % (znacznik, nowych, ile)

    alarmy = ""
    if stoi_pobieranie:
        alarmy += ("<p style='background:#fdecea;padding:10px;border-left:4px solid #b83a3a'>"
                   "<b>Pobieranie stoi.</b> Ostatnia udana synchronizacja była "
                   f"{sync['godzin_od_ok']} godz. temu ({sync['ostatni_ok'] or 'nigdy'}). "
                   "Sprawdź przebiegi „Synchronizacja dzienna” na GitHubie — "
                   "przy pracach serwisowych MF przebieg ginie na limicie czasu.</p>")
    if stoi_streszczanie:
        alarmy += ("<p style='background:#fdecea;padding:10px;border-left:4px solid #b83a3a'>"
                   "<b>Automat streszczający prawdopodobnie stoi.</b> Przez dobę nie powstało "
                   f"żadne streszczenie, a w kolejce czeka {czeka}. Sprawdź stronę "
                   "&bdquo;Streszczanie&rdquo; w Panelu administratora i przebiegi pg_cron.</p>")

    # --- pobieranie ---
    nowe_w = "".join(f"<tr><td>{n['podatek']}</td><td align='right'>{n['ile']}</td></tr>"
                     for n in sync["nowe"]) or "<tr><td colspan='2'>nic nie przybyło</td></tr>"
    przebiegi = "".join(
        f"<tr><td>{str(p['uruchomiono'])[5:16].replace('T', ' ')}</td><td>{p['podatek']}</td>"
        f"<td align='right'>{p['liczba_dok']}</td><td align='right'>{p['nowych_dok']}</td>"
        f"<td>{p['status']}{(' — ' + p['szczegoly'][:60]) if p['szczegoly'] else ''}</td></tr>"
        for p in sync["przebiegi"][:MAKS_W_TABELI]) or \
        "<tr><td colspan='5'>Przez ostatnią dobę nie zapisano żadnego przebiegu synchronizacji.</td></tr>"

    # --- streszczenia ---
    autorzy = "".join(f"<tr><td>{a}</td><td align='right'>{n}</td></tr>"
                      for a, n in stre["autorzy"]) or "<tr><td colspan='2'>—</td></tr>"
    kolejka_w = "".join(f"<tr><td>{k['podatek']}</td><td align='right'>{k['ile']}</td></tr>"
                        for k in stre["kolejka"]) or "<tr><td colspan='2'>pusto</td></tr>"
    wiersze = "".join(
        f"<tr><td>{str(w['wygenerowano'])[11:16]}</td><td>{w['sygnatura']}</td>"
        f"<td>{w['podatek']}</td><td>{_autor(w['zrodlo'])}</td>"
        f"<td>{(w['temat'] or '')[:90]}</td>"
        f"<td align='right'>{w['dl_krotkie']}/{w['dl_pelne']}</td></tr>"
        for w in stre["ostatnie"][:MAKS_W_TABELI]) or \
        "<tr><td colspan='6'>Przez ostatnią dobę nie powstało żadne streszczenie.</td></tr>"

    odlozone = ""
    if stre["odlozone"]:
        pozycje = "".join(f"<li>{o['sygnatura']} — {o['prob']} prób: {o['ostatni_blad'][:120]}</li>"
                          for o in stre["odlozone"])
        odlozone = f"<h3>Odłożone po nieudanych próbach</h3><ul>{pozycje}</ul>"

    braki_w = "".join(
        f"<tr><td>{b['podatek']}</td><td align='right'>{b['brakuje']}</td>"
        f"<td align='right'>{b['miesiecy']}</td></tr>" for b in sync["braki"])
    braki_w = (f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'><tr style='background:#eee'>"
               "<th align='left'>Podatek</th><th>Brakuje</th><th>Miesięcy</th></tr>"
               f"{braki_w}</table>"
               f"<p style='color:#666; font-size:12px'>Razem {sync['brakuje_razem']} "
               "dokumentów. Nocne uzupełnianie bierze najstarszy miesiąc z brakiem; "
               "audyt sprawdza liczby w EURECE i sam wychwyci, gdy MF dopublikuje "
               "coś wstecz.</p>") if sync["braki"] else         "<p>Archiwum kompletne według audytu.</p>"

    tabela = 'border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse"'
    # Tabele z wieloma wierszami dostaja mniejsza czcionke — osobna stala,
    # bo dwa atrybuty style w jednym znaczniku przegladarki czytaja roznie.
    tabela_mala = ('border="1" cellpadding="6" cellspacing="0" '
                   'style="border-collapse:collapse; font-size:12px"')
    html = f"""
    <html><body style="font-family: Arial, sans-serif; font-size: 14px;">
    <h2>DorAIdca Radar — ostatnia doba</h2>
    {alarmy}
    <p><b>Pobrano:</b> {nowych} · <b>streszczono:</b> {ile} · <b>w kolejce:</b> {czeka}</p>

    <h3>📥 Pobieranie z MF</h3>
    <table {tabela}>
      <tr style="background:#eee"><th align="left">Podatek</th><th>Nowych</th></tr>{nowe_w}
    </table>
    <p style="color:#666; font-size:12px">Ostatnia udana synchronizacja:
      <b>{sync['ostatni_ok'] or '—'}</b> ({sync['godzin_od_ok']} godz. temu).</p>
    <table {tabela_mala}>
      <tr style="background:#eee">
        <th>Kiedy</th><th align="left">Podatek</th><th>W oknie</th><th>Nowych</th><th align="left">Status</th>
      </tr>{przebiegi}
    </table>

    <h3>📚 Zaległości w archiwum</h3>
    {braki_w}

    <h3>📝 Streszczenia</h3>
    <table {tabela}>
      <tr style="background:#eee"><th align="left">Autor</th><th>Ile</th></tr>{autorzy}
    </table>

    <h4>Kolejka</h4>
    <table {tabela}>
      <tr style="background:#eee"><th align="left">Podatek</th><th>Czeka</th></tr>{kolejka_w}
    </table>

    {odlozone}

    <h4>Najnowsze streszczenia ({min(ile, MAKS_W_TABELI)})</h4>
    <table {tabela_mala}>
      <tr style="background:#eee">
        <th>Godz.</th><th align="left">Sygnatura</th><th>Podatek</th><th align="left">Autor</th>
        <th align="left">Temat</th><th>Znaków</th>
      </tr>{wiersze}
    </table>

    <p style="color:#666; font-size:12px; margin-top:18px">
      Liczby z bazy w chmurze — tam pisze i synchronizacja, i automat streszczający.
      Do panelu wpisy schodzą synchronizacją o 7:30 i 0:30.
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
    temat, html = zbuduj(zbierz_synchronizacje(db), zbierz_streszczenia(db))
    print(temat)
    if sucho:
        print(html[:2000])
        return 0
    # Nieudana wysyłka kładzie przebieg — cicha awaria poczty jest gorsza od
    # braku maila, bo nikt nie wie, że raport przestał przychodzić.
    return 0 if wyslij(temat, html) else 1


if __name__ == "__main__":
    raise SystemExit(main())
