#!/usr/bin/env python3
"""
zapas_serwera.py — GitHub Actions przejmuje pobieranie z EUREKI, gdy serwer
domowy milczy (od 4.10.2026, decyzja wlasciciela).

KIEDY
  Od 1.10.2026 interpretacje pobiera serwer domowy (okno EUREKI raz na noc),
  a GitHub Actions i baza w chmurze sa zapasem. Gdy padnie sam serwer (prad,
  internet), nikt nie pobiera. Workflow zapas_serwera.yml co 12 godzin
  sprawdza dwa znaki zycia serwera:
    1. przebieg tego workflowu o nazwie „Serwer zyje” — serwer zleca go po
       kazdym oknie EUREKI (workflow_dispatch, tryb zyje);
    2. wiersz 'okno_eureki' w tabeli stan_serwera w chmurze.
  Przejmuje pobieranie dopiero, gdy OBA sa starsze niz 36 godzin. Jeden znak
  moglby zniknac z innego powodu (wygasly token GitHuba, zmienione haslo do
  chmury), a wtedy serwer i Actions pobieralyby z MF podwojnie.

CO PRZEJMUJE
  synchronizacja_dzienna.py do bazy w chmurze — okno PO DACIE PUBLIKACJI od
  dnia przed ostatnim znakiem zycia (albo przed poprzednim przejeciem), wiec
  tylko to, co pojawilo sie w EURECE, gdy serwer milczal — i interpretacje
  ogolne. Najwyzej raz na 20 godzin. Po powrocie serwer sam kopiuje z chmury
  to, co tu pobrano (zadania/okno_eureki.py w repozytorium infrastruktury).
  Zadnych maili: awarie wlasciciel sledzi sam (alarm wylaczony 12.09.2026).

URUCHOMIENIE (w workflow)
  python zapas_serwera.py sprawdz   # GITHUB_OUTPUT: przejmij=1|0, od=RRRR-MM-DD
  python zapas_serwera.py zapisz    # po przejeciu: znacznik w stan_serwera
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

PROG = timedelta(hours=36)
ODSTEP_PRZEJEC = timedelta(hours=20)
NAZWA_ZNAKU = "Serwer żyje"          # run-name przebiegu zleconego przez serwer
WORKFLOW = "zapas_serwera.yml"

# Ta sama definicja co w repozytorium infrastruktury (schemat 47) — kto pierwszy,
# ten zaklada. RLS bez polityk: przez API Supabase tabela jest niewidoczna.
TABELA = """
CREATE TABLE IF NOT EXISTS public.stan_serwera (
    klucz     text PRIMARY KEY,
    dane      jsonb NOT NULL DEFAULT '{}'::jsonb,
    zmieniono timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.stan_serwera ENABLE ROW LEVEL SECURITY;
"""


def teraz() -> datetime:
    return datetime.now(timezone.utc)


def polacz():
    return psycopg2.connect(
        host=os.environ["SUPABASE_HOST"], port=os.environ.get("SUPABASE_PORT") or "5432",
        dbname=os.environ.get("SUPABASE_DB") or "postgres", user=os.environ["SUPABASE_USER"],
        password=os.environ["SUPABASE_PASSWORD"],
        sslmode=os.environ.get("SUPABASE_SSLMODE", "require"), connect_timeout=20)


def wiersz(kur, klucz: str):
    kur.execute("SELECT dane, zmieniono FROM stan_serwera WHERE klucz = %s", (klucz,))
    return kur.fetchone()


def znak_github():
    """Czas ostatniego udanego przebiegu „Serwer zyje” (albo None)."""
    url = ("https://api.github.com/repos/%s/actions/workflows/%s/runs"
           "?event=workflow_dispatch&status=success&per_page=30"
           % (os.environ["GITHUB_REPOSITORY"], WORKFLOW))
    zadanie = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + os.environ.get("GH_TOKEN", ""),
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(zadanie, timeout=30) as r:
        przebiegi = json.load(r).get("workflow_runs") or []
    for p in przebiegi:
        if p.get("display_title") == NAZWA_ZNAKU:
            return datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
    return None


def sprawdz() -> int:
    t = teraz()
    try:
        github = znak_github()
    except Exception as e:
        print("Znak zycia z GitHuba nieodczytany: %s" % e)
        github = None
    with polacz() as pol, pol.cursor() as kur:
        kur.execute(TABELA)
        okno = wiersz(kur, "okno_eureki")
        przejecie = wiersz(kur, "przejecie")
    chmura = okno[1] if okno else None
    print("Ostatni znak zycia serwera — GitHub: %s, chmura: %s" % (github, chmura))
    znaki = [z for z in (github, chmura) if z]
    przejmij, od = False, ""
    if okno and (okno[0] or {}).get("tryb") == "chmura":
        # Pelne przelaczenie na chmure (docs/plan-awaryjny-ban-mf.md): pobieraja
        # zwykle workflowy, przejecie podwoiloby ruch do MF.
        print("Serwer w trybie chmura — pobieraja zwykle workflowy, nic nie robie.")
    elif not znaki:
        # Serwer jeszcze nigdy nie wyslal znaku zycia (np. tuz po wdrozeniu) —
        # nie zgadujemy, nie przejmujemy.
        print("Brak jakiegokolwiek znaku zycia — nic nie robie.")
    elif t - max(znaki) <= PROG:
        print("Serwer zyje (%s temu) — nic nie robie." % str(t - max(znaki)).split(".")[0])
    elif przejecie and t - przejecie[1] < ODSTEP_PRZEJEC:
        print("Serwer milczy, ale pobieranie przejeto %s — nastepne przejecie najwczesniej po 20 h."
              % przejecie[1])
    else:
        ostatnio = max(znaki + ([przejecie[1]] if przejecie else []))
        od = (ostatnio - timedelta(days=1)).date().isoformat()
        przejmij = True
        print("Serwer milczy od %s — przejmuje pobieranie (opublikowane od %s)." % (max(znaki), od))
    wyjscie = os.environ.get("GITHUB_OUTPUT")
    if wyjscie:
        with open(wyjscie, "a", encoding="utf-8") as f:
            f.write("przejmij=%d\nod=%s\n" % (1 if przejmij else 0, od))
    return 0


def zapisz() -> int:
    dane = {"przebieg": os.environ.get("GITHUB_RUN_ID", ""),
            "url": "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", ""),
                                              os.environ.get("GITHUB_REPOSITORY", ""),
                                              os.environ.get("GITHUB_RUN_ID", "")),
            "od": os.environ.get("PUBLIKACJA_OD", "")}
    with polacz() as pol, pol.cursor() as kur:
        kur.execute(TABELA)
        kur.execute("""INSERT INTO stan_serwera (klucz, dane, zmieniono) VALUES ('przejecie', %s, now())
                       ON CONFLICT (klucz) DO UPDATE SET dane = EXCLUDED.dane, zmieniono = now()""",
                    (psycopg2.extras.Json(dane),))
    print("Zapisano przejecie: %s" % dane["url"])
    return 0


if __name__ == "__main__":
    polecenie = sys.argv[1] if len(sys.argv) > 1 else ""
    if polecenie == "sprawdz":
        sys.exit(sprawdz())
    if polecenie == "zapisz":
        sys.exit(zapisz())
    sys.exit("uzycie: zapas_serwera.py sprawdz|zapisz")
