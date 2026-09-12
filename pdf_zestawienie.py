# -*- coding: utf-8 -*-
"""
pdf_zestawienie.py — tygodniowe zestawienie interpretacji w PDF.

Używany przez moduł 6 (Zestawienie Tygodniowe Automat).

===============================================================================
DLACZEGO WYBÓR IDZIE PO DACIE PUBLIKACJI, A NIE PO DACIE WYDANIA
===============================================================================
To jest cała istota tego modułu.

MF publikuje interpretacje z opóźnieniem — sygnatura z 20 lipca potrafi pojawić
się w Eurece 5 sierpnia. Gdyby zestawienie wybierało po `data_wyd`, powstałaby
dziura: generując raport za tydzień 20–26 lipca w poniedziałek 27 lipca, nie
zobaczylibyśmy tej interpretacji, bo jeszcze jej nie było. A gdy się pojawi,
żaden kolejny raport jej nie obejmie, bo jej data wydania należy do przeszłego
tygodnia.

Wybieramy więc po `pobrano_at` — dacie dogrania do bazy. Wtedy KAŻDA
interpretacja trafia do dokładnie jednego zestawienia: tego, w którego tygodniu
faktycznie się pojawiła. Data wydania jest w raporcie pokazana obok i wyróżniona,
gdy odbiega od tygodnia publikacji.

===============================================================================
DLACZEGO PONIEDZIAŁEK–NIEDZIELA, A NIE PONIEDZIAŁEK–PIĄTEK
===============================================================================
Zakres kończący się w piątek zostawia szczelinę: dokument dograny w sobotę albo
niedzielę nie należy ani do tego tygodnia, ani do następnego. Przy siedmiodniowym
oknie tygodnie stykają się bez luki i warunek „nic nie umknie" jest spełniony
konstrukcyjnie, a nie z nadziei, że MF nie publikuje w weekend.
===============================================================================
"""

from __future__ import annotations

import datetime as dt
import io
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (HRFlowable, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

import paleta
# Podział pełnego streszczenia na sekcje — ten sam co na stronie DorAIdca
# i w chmurce wtyczki (METRYKA.naBloki), więc PDF wygląda jak ekran.
import streszczenie_bloki

# Rejestrację fontów bierzemy z modułu Archiwum, zamiast powielać listę ścieżek
# kandydatów. Powielona rozjechałaby się przy pierwszej poprawce, a objawem
# byłyby PDF-y bez polskich znaków — czyli usterka widoczna dopiero u odbiorcy.
import eksplorator_archiwum as _archiwum_ui


# Lista używana WYŁĄCZNIE do grupowania sekcji, gdy generuj() dostaje rekordy
# kilku podatków naraz (podatek=""). Moduł 6 Streamlita zawsze podaje jeden
# podatek, więc go to nie dotyczy — ale zestawienie zbiorcze z nowego
# interfejsu tak: bez PCC pozycje tego podatku wypadłyby z PDF-u po cichu,
# mimo że są w bazie i widać je na ekranie.
PODATKI = ["PIT", "CIT", "VAT", "AKCYZA", "PCC"]


def _fonty() -> tuple[str, str, bool]:
    """Zwraca (regularny, pogrubiony, czy_polskie_znaki_dzialaja)."""
    _archiwum_ui._zarejestruj_fonty()
    return (_archiwum_ui._FONT_REGULAR,
            _archiwum_ui._FONT_BOLD,
            _archiwum_ui._font_polski_ok)


# ---------------------------------------------------------------------------
# TYGODNIE
# ---------------------------------------------------------------------------
def poniedzialek(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def granice_tygodnia(pon: dt.date) -> tuple[str, str]:
    """
    Zakres jako łańcuchy do porównania z pobrano_at.

    Koniec to niedziela 23:59:59, nie piątek — patrz uzasadnienie w nagłówku
    modułu. Porównanie łańcuchowe jest poprawne, bo pobrano_at trzyma czas
    w formacie ISO, a ten sortuje się leksykograficznie tak samo jak
    chronologicznie.
    """
    niedziela = pon + dt.timedelta(days=6)
    return pon.isoformat(), niedziela.isoformat() + "T23:59:59"


def opis_tygodnia(pon: dt.date) -> str:
    niedziela = pon + dt.timedelta(days=6)
    if pon.month == niedziela.month:
        return f"{pon.day}–{niedziela.day} {MIESIACE_DOP[pon.month]} {pon.year}"
    return (f"{pon.day} {MIESIACE_DOP[pon.month]} – "
            f"{niedziela.day} {MIESIACE_DOP[niedziela.month]} {niedziela.year}")


MIESIACE_DOP = {
    1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia", 5: "maja",
    6: "czerwca", 7: "lipca", 8: "sierpnia", 9: "września",
    10: "października", 11: "listopada", 12: "grudnia",
}


def formatuj_date(wartosc) -> str:
    """RRRR-MM-DD albo znacznik czasu ISO -> DD.MM.RRRR."""
    s = str(wartosc or "")[:10]
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return s or "—"


# ---------------------------------------------------------------------------
# TEKST DO PDF
# ---------------------------------------------------------------------------
_ZNACZNIKI = re.compile(r"\*\*(.+?)\*\*")


def _na_akapity(tekst: str) -> list[str]:
    """
    Dzieli tekst na akapity i eskejpuje znaki wymagane przez mini-język
    znacznikowy reportlab. Pogrubienia z markdownu zamieniamy na <b>,
    bo streszczenia z wtyczki i z GPT używają ich w nagłówkach sekcji.
    """
    if not tekst or not str(tekst).strip():
        return []

    wynik = []
    for blok in re.split(r"\n\s*\n", str(tekst).strip()):
        linia = " ".join(l.strip() for l in blok.splitlines() if l.strip())
        if not linia:
            continue
        linia = (linia.replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;"))
        # Zamiana PO eskejpowaniu — inaczej <b> zostałoby zeskejpowane razem
        # z resztą i wyszłoby jako widoczny tekst.
        linia = _ZNACZNIKI.sub(r"<b>\1</b>", linia)
        wynik.append(linia)
    return wynik


# ---------------------------------------------------------------------------
# GENEROWANIE
# ---------------------------------------------------------------------------
def _bloki_pdf(pelne: str, s_sekcja, s_tresc, s_punkt) -> list:
    """Pełne streszczenie po sekcjach: nagłówek wersalikami w kolorze marki
    (jak w chmurce i na stronie), akapity i punktory zamiast jednej ściany.
    Dawny podział po pustych liniach sklejał nagłówek starszych streszczeń
    z pierwszym zdaniem sekcji, a listę — w jedną linię."""
    wynik = []
    for blok in streszczenie_bloki.na_bloki(pelne):
        if blok["naglowek"]:
            wynik.append(Paragraph(streszczenie_bloki.oznacz(blok["naglowek"].upper()), s_sekcja))
        for rodzaj, tresc in streszczenie_bloki.elementy(blok["tresc"]):
            if rodzaj == "ul":
                wynik.extend(Paragraph(streszczenie_bloki.oznacz(t), s_punkt, bulletText="•")
                             for t in tresc)
            else:
                wynik.append(Paragraph(streszczenie_bloki.oznacz(tresc), s_tresc))
    return wynik


def generuj(rekordy: list[dict], pon: dt.date, *,
            z_pelnymi: bool = False, podatek: str = "", w_toku: bool = False) -> bytes:
    """
    Buduje PDF zestawienia tygodniowego.

    rekordy: słowniki z kluczami sygnatura, podatek, data_wyd, pobrano_at,
             temat, streszczenie, streszczenie_pelne, zrodlo, link,
             branze, przedmiot
    z_pelnymi: czy dołączyć pełne streszczenia (znacznie grubszy dokument)
    podatek: gdy podany, tytuł mówi wprost o jednym podatku i pomijamy
             nagłówki sekcji — przy jednorodnej liście byłyby zbędne
    w_toku: tydzień jeszcze trwa — podtytuł mówi to wprost, z chwilą
            wygenerowania, żeby niepełna lista nie udawała kompletnej
    """
    regularny, pogrubiony, _ = _fonty()
    p = paleta.paleta_pdf()

    akcent = colors.HexColor(p["primary"])
    podtekst = colors.HexColor(p["text2"])
    linia_jasna = colors.HexColor(p["border"])
    ostrzezenie = colors.HexColor(p["warning"])

    bufor = io.BytesIO()
    doc = SimpleDocTemplate(
        bufor, pagesize=A4,
        topMargin=2.0 * cm, bottomMargin=1.8 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        title=f"Zestawienie tygodniowe {opis_tygodnia(pon)}",
    )

    s_marka = ParagraphStyle("Marka", fontName=regularny, fontSize=9,
                             textColor=podtekst, spaceAfter=2)
    s_tytul = ParagraphStyle("Tytul", fontName=pogrubiony, fontSize=17,
                             textColor=akcent, spaceAfter=4, leading=21)
    s_podtytul = ParagraphStyle("Podtytul", fontName=regularny, fontSize=10.5,
                                textColor=podtekst, spaceAfter=14, leading=15)
    s_podatek = ParagraphStyle("Podatek", fontName=pogrubiony, fontSize=13,
                               textColor=akcent, spaceBefore=14, spaceAfter=8)
    s_sygnatura = ParagraphStyle("Sygnatura", fontName=pogrubiony, fontSize=10.5,
                                 textColor=colors.HexColor(p["text"]),
                                 spaceBefore=10, spaceAfter=2)
    s_meta = ParagraphStyle("Meta", fontName=regularny, fontSize=8.5,
                            textColor=podtekst, spaceAfter=5, leading=12)
    s_meta_uwaga = ParagraphStyle("MetaUwaga", parent=s_meta,
                                  textColor=ostrzezenie)
    s_temat = ParagraphStyle("Temat", fontName=pogrubiony, fontSize=9.5,
                             textColor=colors.HexColor(p["text"]),
                             spaceAfter=4, leading=13)
    # Klasyfikacja stoi NAD tematem, nie w linii meta z datami. Powód: to
    # informacja merytoryczna („czego dotyczy"), a nie techniczna („kiedy
    # wpadło"). Wrzucona między daty ginie; postawiona nad tematem czyta się
    # jako etykieta całej pozycji i pozwala przejrzeć zestawienie po obszarach
    # bez czytania streszczeń.
    s_klasyfikacja = ParagraphStyle("Klasyfikacja", fontName=pogrubiony,
                                    fontSize=8, textColor=akcent,
                                    spaceBefore=2, spaceAfter=3, leading=11)
    s_tresc = ParagraphStyle("Tresc", fontName=regularny, fontSize=9.3,
                             leading=13.5, spaceAfter=6, alignment=4,
                             textColor=colors.HexColor(p["text"]))
    s_brak = ParagraphStyle("Brak", parent=s_tresc, textColor=ostrzezenie)
    # Pełne streszczenie: nagłówek sekcji jak w chmurce — mały, pogrubiony,
    # wersalikami, w kolorze marki; punkty z wcięciem.
    s_sekcja = ParagraphStyle("Sekcja", fontName=pogrubiony, fontSize=7.8,
                              textColor=akcent, spaceBefore=7, spaceAfter=2.5,
                              leading=10)
    s_punkt = ParagraphStyle("Punkt", parent=s_tresc, leftIndent=11, bulletIndent=2,
                             bulletFontName=regularny, spaceAfter=2.5, alignment=0)
    s_stopka = ParagraphStyle("Stopka", fontName=regularny, fontSize=8,
                              textColor=podtekst, spaceBefore=4, leading=11)

    e = []
    e.append(Paragraph(f"{paleta.NAZWA_MARKI} — zestawienie tygodniowe", s_marka))
    e.append(HRFlowable(width="100%", thickness=1.1, color=akcent, spaceAfter=10))
    tytul = (f"{podatek} — interpretacje z tygodnia {opis_tygodnia(pon)}"
             if podatek else f"Interpretacje z tygodnia {opis_tygodnia(pon)}")
    e.append(Paragraph(tytul, s_tytul))

    # Liczby w podtytule mówią wprost, czego dotyczy zestawienie — bez tego
    # czytelnik nie wie, czy zero pozycji dla podatku znaczy „nic nie było",
    # czy „coś się nie wygenerowało".
    opoznione = sum(1 for r in rekordy if _publikacja_opozniona(r, pon))
    czesci = [f"Pozycji: {len(rekordy)}"]
    if opoznione:
        czesci.append(f"w tym {opoznione} z wcześniejszą datą wydania")
    czesci.append("wybór po dacie publikacji w bazie")
    if w_toku:
        czesci.append(f"tydzień w toku — stan na {dt.datetime.now():%d.%m.%Y, %H:%M}")
    e.append(Paragraph(" · ".join(czesci), s_podtytul))

    if not rekordy:
        e.append(Paragraph(
            f"W tym tygodniu nie pojawiła się w bazie żadna interpretacja"
            f"{' ' + podatek if podatek else ''}.", s_brak))
    else:
        # Przy zestawieniu jednego podatku nagłówki sekcji są zbędne —
        # wszystkie pozycje należą do tej samej grupy, a tytuł już o tym mówi.
        # Podatki dodane z EUREKI (np. CUKIER) ida po wbudowanej piatce —
        # inaczej ich pozycje wypadlyby z PDF-u tak samo, jak kiedys PCC.
        kolejnosc = PODATKI + sorted({(r.get("podatek") or "") for r in rekordy} - set(PODATKI))
        grupy = ([(podatek, rekordy)] if podatek else
                 [(p, [r for r in rekordy if (r.get("podatek") or "") == p])
                  for p in kolejnosc])

        for nazwa_grupy, grupa in grupy:
            if not grupa:
                continue

            if not podatek:
                e.append(Paragraph(f"{nazwa_grupy} — {len(grupa)}", s_podatek))
                e.append(HRFlowable(width="100%", thickness=0.7,
                                    color=linia_jasna, spaceAfter=6))

            for r in grupa:
                e.append(Paragraph(str(r.get("sygnatura") or "—"), s_sygnatura))

                meta = [f"Wydano: {formatuj_date(r.get('data_wyd'))}",
                        f"Opublikowano: {formatuj_date(r.get('pobrano_at'))}"]
                zrodlo = str(r.get("zrodlo") or "")
                if zrodlo.startswith("wtyczka"):
                    meta.append("streszczenie z wtyczki")
                elif zrodlo.startswith("gpt"):
                    meta.append("streszczenie z GPT")

                styl = s_meta_uwaga if _publikacja_opozniona(r, pon) else s_meta
                e.append(Paragraph(" · ".join(meta), styl))

                klasyfikacja = _linia_klasyfikacji(r)
                if klasyfikacja:
                    e.append(Paragraph(klasyfikacja, s_klasyfikacja))

                if r.get("temat"):
                    e.append(Paragraph(str(r["temat"]), s_temat))

                streszczenie = (r.get("streszczenie") or "").strip()
                if streszczenie:
                    for akapit in _na_akapity(streszczenie):
                        e.append(Paragraph(akapit, s_tresc))
                else:
                    e.append(Paragraph(
                        "Brak streszczenia — interpretacja czeka na przetworzenie.",
                        s_brak))

                if z_pelnymi and (r.get("streszczenie_pelne") or "").strip():
                    e.append(Spacer(1, 4))
                    e.extend(_bloki_pdf(r["streszczenie_pelne"], s_sekcja, s_tresc, s_punkt))

                if r.get("link"):
                    e.append(Paragraph(
                        f"Źródło: {str(r['link']).replace('&', '&amp;')}", s_stopka))

    e.append(Spacer(1, 12))
    e.append(HRFlowable(width="100%", thickness=0.6, color=linia_jasna, spaceAfter=6))
    e.append(Paragraph(
        f"Wygenerowano {dt.datetime.now():%d.%m.%Y o %H:%M}. "
        "Zestawienie obejmuje interpretacje, które pojawiły się w bazie w podanym "
        "tygodniu — niezależnie od daty ich wydania. Przed wykorzystaniem "
        "zweryfikuj aktualność.", s_stopka))

    doc.build(e)
    return bufor.getvalue()


def _linia_klasyfikacji(rekord: dict) -> str:
    """Linia „Branża · Przedmiot” nad tematem. Pusta, gdy brak obu wartości.

    Pokazujemy to, co jest — jedna z wartości potrafi być pusta, bo GPT gubi
    klasyfikację przy dłuższych sesjach. Wypisanie samej dostępnej połowy jest
    użyteczniejsze niż ukrycie całej linii albo drukowanie myślnika w miejscu
    brakującej wartości.

    Branż bywa kilka (rozdzielone przecinkiem), przedmiot jest jeden —
    obie wartości wchodzą tu w tej postaci, w jakiej stoją w bazie.
    """
    czesci = []
    branze = (rekord.get("branze") or "").strip()
    przedmiot = (rekord.get("przedmiot") or "").strip()
    if branze:
        czesci.append(branze)
    if przedmiot:
        czesci.append(przedmiot)
    if not czesci:
        return ""
    # Eskejpowanie takie samo jak w _na_akapity — wartości pochodzą z taksonomii,
    # ale zakładanie, że nigdy nie zawierają & ani <, jest proszeniem się
    # o rozsypany PDF przy pierwszej nietypowej pozycji.
    return " · ".join(czesci).replace("&", "&amp;") \
                             .replace("<", "&lt;").replace(">", "&gt;")


def _publikacja_opozniona(rekord: dict, pon: dt.date) -> bool:
    """Czy interpretacja została wydana przed tygodniem, w którym się pojawiła?"""
    try:
        wydano = dt.date.fromisoformat(str(rekord.get("data_wyd") or "")[:10])
    except Exception:
        return False
    return wydano < pon
