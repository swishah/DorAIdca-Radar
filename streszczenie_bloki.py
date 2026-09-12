# -*- coding: utf-8 -*-
"""
streszczenie_bloki.py — pełne streszczenie podzielone na sekcje.

TEN SAM PODZIAŁ CO WE WTYCZCE
  Port 1:1 funkcji METRYKA.naBloki z metryka.js (chmurka i okienko wtyczki).
  Nagłówek sekcji to linia w całości pogrubiona (**Opis stanu faktycznego**)
  albo DOKŁADNA nazwa z zamkniętej listy — starsze streszczenia mają nagłówki
  jako zwykły tekst i bez tej listy zlewały się w jedną ścianę. Ślady
  interfejsu czatu przed pierwszym nagłówkiem wypadają.

  Korzystają z niego strona DorAIdca (Pulpit, Zestawienie) i generator PDF
  zestawienia. Jeden podział w trzech miejscach, więc to samo streszczenie
  wygląda wszędzie tak samo. Zmiana reguł tutaj = ta sama zmiana w metryka.js.

TREŚĆ SEKCJI
  Każda niepusta linia to osobny akapit (model nie łamie wierszy w środku
  zdania, za to „Pytanie 1: …" i „Pytanie 2: …" w kolejnych liniach to dwie
  osobne rzeczy). Linie od „- ", „• ", „* " składają się w listę punktowaną.
  Numeracja „1." zostaje w tekście — to część treści, nie ozdoba.

  Pogrubienia **tak** zamienia oznacz() — na <strong> dla strony, na <b> dla
  reportlaba. Pozostały tekst jest eskejpowany, więc treść z bazy nie wstrzyknie
  znaczników ani do HTML-a, ani do PDF-a.
"""

from __future__ import annotations

import re

NAZWY_SEKCJI = {
    "podatek", "podatek i temat", "sygnatura i rodzaj", "w jednym zdaniu",
    "opis stanu faktycznego", "stan faktyczny",
    "pytania", "pytanie i stanowisko", "problem prawny",
    "stanowisko podatnika", "stanowisko wnioskodawcy", "stanowisko skarzacego",
    "stanowisko wnioskodawcyskarzacego",
    "stanowisko organu", "stanowisko organu podatkowego",
    "rozstrzygniecie", "uzasadnienie", "tok rozumowania",
    "podstawy prawne", "najwazniejsze podstawy prawne", "podstawa prawna",
    "znaczenie praktyczne", "ryzyka i ograniczenia",
    "odwolania do innego orzecznictwa", "do wykorzystania w pismie",
}

_OGONKI = str.maketrans("ąàáâćçęèéêłńóòôśźż", "aaaacceeeelnooosz" + "z")
_RE_POGRUBIONA_LINIA = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?\s*$")
_RE_METRYKA = re.compile(r"-{2,}\s*METRYKA\s*-{2,}", re.I)
_RE_PUNKT = re.compile(r"^\s*[-•*·–]\s+")
_RE_POGRUBIENIE = re.compile(r"\*\*(.+?)\*\*")

# Ślady interfejsu czatu, które potrafią wejść do odpowiedzi razem z treścią
# („Analizował interpretację podatkową i strukturyzował rozstrzygnięcie").
_SLADY_CZATU = [re.compile(w, re.I) for w in (
    r"^analizowa[łl]", r"^przemy[śs]la", r"^my[śs]l[ea]", r"^rozwa[żz]a",
    r"^strukturyzowa", r"^przetwarzano", r"^zastanawia")]


def klucz_sekcji(s: str) -> str:
    """Klucz porównania: bez ogonków, bez interpunkcji, małymi literami."""
    s = str(s or "").replace("**", "").strip().lower().translate(_OGONKI)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def naglowek_sekcji(linia: str) -> str | None:
    """Nazwa sekcji albo None. Dwie drogi: pogrubienie albo nazwa z listy."""
    m = _RE_POGRUBIONA_LINIA.match(linia or "")
    if m:
        return re.sub(r"\s*:\s*$", "", m.group(1).strip())
    goly = re.sub(r"\s*:\s*$", "", str(linia or "").strip())
    if goly and len(goly) < 60 and klucz_sekcji(goly) in NAZWY_SEKCJI:
        return goly
    return None


def slad_czatu(linia: str) -> bool:
    s = str(linia or "").strip()
    return bool(s) and len(s) <= 120 and any(w.search(s) for w in _SLADY_CZATU)


def na_bloki(pelne: str) -> list[dict]:
    """[{"naglowek": str, "tresc": str}] — jak METRYKA.naBloki."""
    tekst = str(pelne or "")
    # Starsze wpisy bywają zapisane razem z blokiem ---METRYKA--- (to dane
    # dla zapisu, nie treść do czytania) — ucinamy go, jak METRYKA.bezMetryki.
    m = _RE_METRYKA.search(tekst)
    if m:
        tekst = tekst[:m.start()]

    bloki, biezacy, byl_naglowek = [], {"naglowek": "", "linie": []}, False
    for linia in tekst.split("\n"):
        n = naglowek_sekcji(linia)
        if n:
            if biezacy["naglowek"] or "".join(biezacy["linie"]).strip():
                bloki.append(biezacy)
            biezacy, byl_naglowek = {"naglowek": n, "linie": []}, True
        else:
            # Ślady czatu odsiewamy TYLKO przed pierwszym nagłówkiem — dalej
            # mogłyby to być prawdziwe zdania zaczynające się podobnie.
            if not byl_naglowek and slad_czatu(linia):
                continue
            biezacy["linie"].append(linia)
    if biezacy["naglowek"] or "".join(biezacy["linie"]).strip():
        bloki.append(biezacy)

    wynik = [{"naglowek": b["naglowek"], "tresc": "\n".join(b["linie"]).strip()} for b in bloki]
    return [b for b in wynik if b["naglowek"] or b["tresc"]]


def elementy(tresc: str) -> list[tuple[str, object]]:
    """Treść sekcji: [("p", tekst), ("ul", [tekst, …]), …]."""
    wynik, lista = [], []
    for linia in str(tresc or "").split("\n"):
        s = linia.strip()
        if not s:
            if lista:
                wynik.append(("ul", lista))
                lista = []
            continue
        m = _RE_PUNKT.match(s)
        if m:
            lista.append(s[m.end():].strip())
            continue
        if lista and linia[:1] in (" ", "\t"):
            lista[-1] += " " + s          # wcięta kontynuacja punktu
            continue
        if lista:
            wynik.append(("ul", lista))
            lista = []
        wynik.append(("p", s))
    if lista:
        wynik.append(("ul", lista))
    return wynik


def oznacz(tekst: str, znacznik: str = "b") -> str:
    """Eskejpowanie &, <, > i **pogrubienia** jako <znacznik>…</znacznik>."""
    t = str(tekst or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _RE_POGRUBIENIE.sub(r"<%s>\1</%s>" % (znacznik, znacznik), t)
