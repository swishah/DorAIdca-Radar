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

# --- wybor okna --------------------------------------------------------------
pod = {"PIT": {"nastepne_od": "2023-01-01", "koniec": "2026-06-21"},
       "CIT": {"nastepne_od": "2023-02-10", "koniec": "2023-02-15"},
       "VAT": {"nastepne_od": "2024-01-03", "koniec": "2024-01-02"}}
assert n.nastepne_okno(pod) == ("PIT", "2023-01-01", "2023-01-31")
pod["PIT"]["nastepne_od"] = "2023-02-11"
# Najwczesniejszy niepobrany miesiac wygrywa niezaleznie od podatku.
assert n.nastepne_okno(pod) == ("CIT", "2023-02-10", "2023-02-15")
pod["CIT"]["nastepne_od"] = "2023-02-16"
assert n.nastepne_okno(pod) == ("PIT", "2023-02-11", "2023-02-28")
# VAT ma nastepne_od za koniec — jest zrobiony i nie wraca do kolejki.
pod["PIT"]["nastepne_od"] = "2026-06-22"
assert n.nastepne_okno(pod) is None
# Okno nigdy nie przekracza konca planu.
assert n.nastepne_okno({"X": {"nastepne_od": "2023-01-01", "koniec": "2023-01-10"}}) \
       == ("X", "2023-01-01", "2023-01-10")

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
