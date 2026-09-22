# -*- coding: utf-8 -*-
"""Test offline uzupelnianie_nocne.py — bez bazy, bez MF, bez GitHuba.

Pilnuje tego, co przy pomylce kosztuje najwiecej: wyboru okna (pominiety
miesiac zostalby pominiety na zawsze), budzetu nocy i wstrzymania po oznace
blokady MF.
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- atrapy przed importem ---------------------------------------------------
os.environ.setdefault("SUPABASE_HOST", "x")
os.environ.setdefault("SUPABASE_USER", "x")
os.environ.setdefault("SUPABASE_PASSWORD", "x")

sys.modules.setdefault("db_core", types.ModuleType("db_core"))
sys.modules["db_core"].SupabaseDB = lambda *a, **k: None
mf = types.ModuleType("uzupelnianie_mf")
mf.KATALOG = "wynik_test"
mf.main = lambda: None
sys.modules["uzupelnianie_mf"] = mf

import uzupelnianie_nocne as n

# --- wybor okna z audytu kompletnosci ---
class BazaAtrapa:
    """Oddaje to, co widok braki_archiwum — bez bazy."""
    def __init__(self, wiersze): self.wiersze, self.zapisy = wiersze, []
    def wykonaj(self, sql, params=None, fetch=False):
        if fetch:
            return self.wiersze
        self.zapisy.append((sql, params))
        return 1


# Najstarszy miesiac wygrywa niezaleznie od podatku — chodzi o to, zeby
# archiwum zapelnialo sie od dolu, a nie podatek po podatku.
db = BazaAtrapa([{"podatek": "VAT", "miesiac": "2024-02", "brakuje": 824}])
assert n.nastepne_okno(db) == ("VAT", "2024-02", "2024-02-01", "2024-02-29", 824)

# Grudzien: okno konczy sie 31.12, a nie przechodzi na styczen.
db = BazaAtrapa([{"podatek": "PIT", "miesiac": "2023-12", "brakuje": 5}])
assert n.nastepne_okno(db) == ("PIT", "2023-12", "2023-12-01", "2023-12-31", 5)

# Luty roku przestepnego ma 29 dni — inaczej ostatni dzien wypadlby z okna.
db = BazaAtrapa([{"podatek": "CIT", "miesiac": "2024-02", "brakuje": 1}])
assert n.nastepne_okno(db)[3] == "2024-02-29"

# Brak brakow = koniec pracy.
assert n.nastepne_okno(BazaAtrapa([])) is None

# Miesiac odwiedzony tej nocy nie wraca, nawet gdy nic z niego nie pobrano.
# Bez tego pobrane=0 krecilo petla na tym samym miesiacu przez cztery godziny.
class BazaZWidokiem(BazaAtrapa):
    """Jak widok braki_archiwum: filtruje po liscie pominietych."""
    def wykonaj(self, sql, params=None, fetch=False):
        if fetch:
            pomin = set(params[0]) if params else set()
            return [w for w in self.wiersze
                    if "%s|%s" % (w["podatek"], w["miesiac"]) not in pomin][:1]
        return super().wykonaj(sql, params, fetch)

db = BazaZWidokiem([{"podatek": "VAT", "miesiac": "2024-02", "brakuje": 3},
                    {"podatek": "VAT", "miesiac": "2024-03", "brakuje": 9}])
assert n.nastepne_okno(db)[1] == "2024-02"
assert n.nastepne_okno(db, ["VAT|2024-02"])[1] == "2024-03"
assert n.nastepne_okno(db, ["VAT|2024-02", "VAT|2024-03"]) is None

# Postep zapisuje sie w audycie, zanim Docker potwierdzi wlasnym licznikiem.
db = BazaAtrapa([])
n.zanotuj_pobranie(db, "VAT", "2024-02", 400)
assert db.zapisy and db.zapisy[0][1] == ("VAT", "2024-02", 400), db.zapisy

# --- budzet nocy -------------------------------------------------------------
# Przebiegi 23:45–02:45 UTC naleza do TEJ SAMEJ nocy polskiej — inaczej limit
# 1500 zerowalby sie w polowie okna i noc pobralaby dwa razy tyle.
wieczor = datetime(2026, 9, 21, 23, 45, tzinfo=timezone.utc)
nad_ranem = datetime(2026, 9, 22, 2, 45, tzinfo=timezone.utc)
assert wieczor.astimezone(n.PL).date() == nad_ranem.astimezone(n.PL).date()

stan = {"noc": {"data": wieczor.astimezone(n.PL).date().isoformat(), "pobrane": 400}}
assert n.pobrane_tej_nocy(stan, wieczor) == 400
assert n.pobrane_tej_nocy(stan, nad_ranem) == 400
# Kolejna noc zaczyna liczenie od zera.
assert n.pobrane_tej_nocy(stan, nad_ranem + timedelta(days=1)) == 0
assert n.pobrane_tej_nocy({}, wieczor) == 0

# --- limity ------------------------------------------------------------------
assert n.MAKS_PRZEBIEGU <= n.MAKS_NOCY
assert n.WSTRZYMANIE == timedelta(hours=24)

print("uzupelnianie_nocne.py: OK")
