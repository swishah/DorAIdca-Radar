"""
DorAIdca Radar CFO Analyzer — v3
Pełna analiza wskaźnikowa z e-Sprawozdań KRS (XML/XAdES/HTML).
~50 wskaźników w 6 grupach, branżowe progi, encyklopedia, eksport Excel.
"""

import streamlit as st
import pandas as pd
import io
import re
from bs4 import BeautifulSoup
from datetime import datetime

try:
    st.set_page_config(page_title="DorAIdca Radar CFO", page_icon="📈", layout="wide")
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# 1. BAZA WIEDZY O WSKAŹNIKACH
#    Każdy wskaźnik to słownik z pełnym opisem, wzorem, interpretacją branżową.
# ═══════════════════════════════════════════════════════════════════════════════

BRANZE = [
    "Ogólna (domyślna)",
    "Handel detaliczny / e-commerce",
    "Produkcja przemysłowa",
    "Budownictwo i deweloperka",
    "Usługi IT / SaaS",
    "Usługi finansowe / Ubezpieczenia",
    "Energetyka i utilities",
    "Transport i logistyka",
    "Ochrona zdrowia",
    "Rolnictwo i spożywczy",
    "Hotelarstwo i gastronomia",
]

# Progi interpretacji wg branży: {id_wskaznika: {branza: (min_ok, max_ok, min_warn, max_warn)}}
# Schemat: (dolna_granica_OK, górna_granica_OK, dolna_ostrzeżenie, górna_ostrzeżenie)
PROGI_BRANZOWE = {
    "current_ratio": {
        "Ogólna (domyślna)":             (1.2, 2.0, 1.0, 3.0),
        "Handel detaliczny / e-commerce": (0.9, 1.5, 0.7, 2.5),
        "Produkcja przemysłowa":           (1.3, 2.5, 1.0, 3.5),
        "Budownictwo i deweloperka":       (1.5, 3.0, 1.2, 4.0),
        "Usługi IT / SaaS":               (1.5, 4.0, 1.2, 6.0),
        "Usługi finansowe / Ubezpieczenia":(1.0, 2.0, 0.8, 3.0),
        "Energetyka i utilities":          (1.0, 1.8, 0.8, 2.5),
        "Transport i logistyka":           (1.1, 2.0, 0.9, 3.0),
        "Ochrona zdrowia":                 (1.2, 2.5, 1.0, 3.5),
        "Rolnictwo i spożywczy":           (1.3, 2.5, 1.0, 4.0),
        "Hotelarstwo i gastronomia":       (0.8, 1.5, 0.6, 2.5),
    },
    "ros": {
        "Ogólna (domyślna)":              (3.0, 999, 0.0, 999),
        "Handel detaliczny / e-commerce":  (1.0, 999, 0.0, 999),
        "Produkcja przemysłowa":            (4.0, 999, 1.0, 999),
        "Budownictwo i deweloperka":        (5.0, 999, 2.0, 999),
        "Usługi IT / SaaS":               (15.0, 999, 5.0, 999),
        "Usługi finansowe / Ubezpieczenia":(10.0, 999, 3.0, 999),
        "Energetyka i utilities":           (8.0, 999, 3.0, 999),
        "Transport i logistyka":            (3.0, 999, 1.0, 999),
        "Ochrona zdrowia":                  (5.0, 999, 2.0, 999),
        "Rolnictwo i spożywczy":            (2.0, 999, 0.5, 999),
        "Hotelarstwo i gastronomia":        (3.0, 999, 1.0, 999),
    },
    "debt_ratio": {
        "Ogólna (domyślna)":              (0.0, 60.0, 0.0, 75.0),
        "Handel detaliczny / e-commerce":  (0.0, 65.0, 0.0, 80.0),
        "Produkcja przemysłowa":            (0.0, 55.0, 0.0, 70.0),
        "Budownictwo i deweloperka":        (0.0, 70.0, 0.0, 85.0),
        "Usługi IT / SaaS":               (0.0, 45.0, 0.0, 60.0),
        "Usługi finansowe / Ubezpieczenia":(0.0, 85.0, 0.0, 92.0),
        "Energetyka i utilities":           (0.0, 65.0, 0.0, 78.0),
        "Transport i logistyka":            (0.0, 65.0, 0.0, 80.0),
        "Ochrona zdrowia":                  (0.0, 55.0, 0.0, 70.0),
        "Rolnictwo i spożywczy":            (0.0, 60.0, 0.0, 75.0),
        "Hotelarstwo i gastronomia":        (0.0, 70.0, 0.0, 85.0),
    },
}

# Pełna encyklopedia wskaźników
WSKAZNIKI_DEFINICJE = {

    # ── PŁYNNOŚĆ ─────────────────────────────────────────────────────────────
    "current_ratio": {
        "id": "current_ratio",
        "nazwa": "Płynność bieżąca (Current Ratio)",
        "grupa": "Płynność",
        "wzor": "Aktywa obrotowe / Zobowiązania krótkoterminowe",
        "wzor_latex": "CR = \\frac{Aktywa\\ obrotowe}{Zobowiązania\\ krótkoterminowe}",
        "opis": (
            "Najbardziej podstawowy miernik zdolności firmy do regulowania "
            "bieżących zobowiązań. Pokazuje, ile razy majątek obrotowy pokrywa "
            "krótkoterminowe długi. Wartość < 1 oznacza, że firma nie ma wystarczających "
            "aktywów, żeby spłacić wszystkie bieżące zobowiązania naraz."
        ),
        "interpretacja_ogolna": (
            "Norma dla większości branż: 1,2 – 2,0. Poniżej 1,0 to sygnał alarmowy — "
            "firma może mieć problem z płynnością. Powyżej 3,0 sugeruje nadmierną "
            "immobilizację kapitału (gotówka leży bezczynnie)."
        ),
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Handel żyje na krótkich cyklach — CR = 0,9–1,5 jest normą, bo dostawcy udzielają długich kredytów kupieckich.",
            "Usługi IT / SaaS": "Firmy SaaS mają małe zapasy, więc CR może być wysoki (3–6). Kluczowy jest raczej Quick Ratio.",
            "Budownictwo i deweloperka": "Wysokie zapasy (materiały, grunty) zawyżają CR. Bezpieczna norma: 1,5–3,0.",
            "Usługi finansowe / Ubezpieczenia": "Branża regulowana — niskie CR nie musi być problemem, nadzór wymaga innych wskaźników.",
        },
    },
    "quick_ratio": {
        "id": "quick_ratio",
        "nazwa": "Płynność szybka (Quick Ratio / Acid Test)",
        "grupa": "Płynność",
        "wzor": "(Aktywa obrotowe − Zapasy) / Zobowiązania krótkoterminowe",
        "wzor_latex": "QR = \\frac{Aktywa\\ obrotowe - Zapasy}{Zobowiązania\\ krótkoterminowe}",
        "opis": (
            "Ostra wersja Current Ratio — wyklucza zapasy, bo ich upłynnienie jest "
            "najwolniejsze. Mierzy, czy firma może spłacić długi bez wyprzedaży magazynu. "
            "Szczególnie ważny dla firm z wysokim udziałem zapasów (produkcja, handel)."
        ),
        "interpretacja_ogolna": "Bezpieczny poziom: ≥ 1,0. Poniżej 0,7 to sygnał ostrzegawczy.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "QR < 0,5 jest tu normą — handel żyje ze sprzedaży zapasów. Analizuj trend, nie poziom.",
            "Usługi IT / SaaS": "Firmy bez zapasów mają QR = CR. Oczekiwany zakres: > 1,5.",
            "Produkcja przemysłowa": "QR = 0,8–1,2 jest typowe. Poniżej 0,6 warto sprawdzić rotację zapasów.",
        },
    },
    "cash_ratio": {
        "id": "cash_ratio",
        "nazwa": "Płynność gotówkowa (Cash Ratio)",
        "grupa": "Płynność",
        "wzor": "Inwestycje krótkoterminowe / Zobowiązania krótkoterminowe",
        "wzor_latex": "CashR = \\frac{Środki\\ pieniężne\\ i\\ ekwiwalenty}{Zobowiązania\\ krótkoterminowe}",
        "opis": (
            "Najbardziej konserwatywna miara płynności — tylko gotówka i jej ekwiwalenty. "
            "Pokazuje, jaką część długów firma może spłacić natychmiast, bez sprzedaży "
            "czegokolwiek. Kluczowy dla oceny ryzyka w scenariuszach kryzysowych."
        ),
        "interpretacja_ogolna": "Bezpieczny poziom: ≥ 0,2. Poniżej 0,1 firma żyje 'na kredycie'. Powyżej 0,5 może świadczyć o nieefektywnym zarządzaniu gotówką.",
        "uwagi_branzowe": {
            "Usługi finansowe / Ubezpieczenia": "Wymogi regulacyjne wymuszają wysokie rezerwy gotówki. Norma: > 0,3.",
            "Hotelarstwo i gastronomia": "Wysoka sezonowość — Cash Ratio silnie waha się w ciągu roku. Analizuj śródroczne dane.",
        },
    },
    "nwc_ratio": {
        "id": "nwc_ratio",
        "nazwa": "Kapitał obrotowy netto do aktywów (NWC Ratio) %",
        "grupa": "Płynność",
        "wzor": "(Aktywa obrotowe − Zobowiązania krótkoterminowe) / Aktywa razem × 100",
        "wzor_latex": "NWC\\_Ratio = \\frac{Aktywa\\ obrotowe - Zob.\\ krótk.}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Pokazuje, jaka część majątku firmy finansowana jest 'bezpiecznie' — "
            "kapitałem długoterminowym. Ujemny NWC = firma finansuje długoterminowe "
            "aktywa krótkoterminowym długiem. Bardzo ryzykowne."
        ),
        "interpretacja_ogolna": "Powinien być dodatni (> 5%). Ujemny to sygnał strukturalnej niestabilności.",
        "uwagi_branzowe": {},
    },
    "defensive_interval": {
        "id": "defensive_interval",
        "nazwa": "Wskaźnik okresu obronnego (Defensive Interval) — dni",
        "grupa": "Płynność",
        "wzor": "(Środki pieniężne + Należności) / (Koszty operacyjne / 365)",
        "wzor_latex": "DI = \\frac{Gotówka + Należności}{Koszty\\ operacyjne / 365}",
        "opis": (
            "Odpowiada na pytanie: ile dni firma przetrwa bez nowych przychodów, "
            "opłacając bieżące koszty z posiadanych płynnych aktywów? "
            "Analitycy ryzyka traktują go jako 'runway' finansowy."
        ),
        "interpretacja_ogolna": "Im więcej dni tym lepiej. Minimum bezpieczeństwa to 30 dni, komfort to 60+ dni.",
        "uwagi_branzowe": {
            "Usługi IT / SaaS": "Startupy SaaS powinny celować w > 90 dni (runway).",
            "Hotelarstwo i gastronomia": "Branża sezonowa — minimum 60 dni buforu.",
        },
    },

    # ── RENTOWNOŚĆ ───────────────────────────────────────────────────────────
    "ros": {
        "id": "ros",
        "nazwa": "Rentowność sprzedaży netto (ROS / Net Profit Margin) %",
        "grupa": "Rentowność",
        "wzor": "Zysk netto / Przychody ze sprzedaży × 100",
        "wzor_latex": "ROS = \\frac{Zysk\\ netto}{Przychody\\ ze\\ sprzedaży} \\times 100",
        "opis": (
            "Pokazuje, ile groszy czystego zysku firma zarabia z każdej złotówki "
            "przychodu. Kluczowy miernik efektywności całościowej działalności — "
            "uwzględnia wszystkie koszty łącznie z podatkiem i odsetkami."
        ),
        "interpretacja_ogolna": "Branżowe mediany: handel 1–3%, produkcja 4–8%, IT/SaaS 15–30%, usługi finansowe 10–20%.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Marże 1–3% są normą przy wysokich obrotach. Kluczowe są wolumen i rotacja.",
            "Usługi IT / SaaS": "Dojrzały SaaS powinien osiągać > 20% ROS. Poniżej 10% przy dużej bazie klientów to sygnał problemu z kosztami.",
            "Budownictwo i deweloperka": "Wahania marż między projektami są ogromne — analizuj w perspektywie kilkuletniej.",
        },
    },
    "roa": {
        "id": "roa",
        "nazwa": "Rentowność aktywów (ROA) %",
        "grupa": "Rentowność",
        "wzor": "Zysk netto / Aktywa razem × 100",
        "wzor_latex": "ROA = \\frac{Zysk\\ netto}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Mierzy, jak efektywnie firma wykorzystuje cały swój majątek do "
            "generowania zysku. Jest niezależny od struktury finansowania — "
            "porównuje firmy niezależnie od dźwigni finansowej."
        ),
        "interpretacja_ogolna": "Dobry ROA: > 5%. Poniżej 2% sugeruje słabą efektywność aktywów. Porównuj zawsze z branżą.",
        "uwagi_branzowe": {
            "Energetyka i utilities": "Branża kapitałochłonna — ROA = 2–4% jest normą przy ogromnych aktywach trwałych.",
            "Usługi IT / SaaS": "Niskie aktywa trwałe → ROA często > 15%. Poniżej 8% to sygnał ostrzegawczy.",
            "Hotelarstwo i gastronomia": "Duże inwestycje w nieruchomości obniżają ROA. Norma: 2–6%.",
        },
    },
    "roe": {
        "id": "roe",
        "nazwa": "Rentowność kapitału własnego (ROE) %",
        "grupa": "Rentowność",
        "wzor": "Zysk netto / Kapitał własny × 100",
        "wzor_latex": "ROE = \\frac{Zysk\\ netto}{Kapitał\\ własny} \\times 100",
        "opis": (
            "Miara stopy zwrotu dla właścicieli/akcjonariuszy. Pokazuje, ile zarabiają "
            "na każdej złotówce zainwestowanego kapitału. Buffett szuka firm z ROE > 15% "
            "przez wiele lat — to oznaka trwałej przewagi konkurencyjnej (moat)."
        ),
        "interpretacja_ogolna": "Cel inwestorski: > 15%. Bardzo dobry: > 20%. Poniżej kosztu kapitału (ok. 8–10%) firma niszczy wartość.",
        "uwagi_branzowe": {
            "Usługi finansowe / Ubezpieczenia": "Banki z ROE > 12% są efektywne. Poniżej 8% to słaba jakość zarządzania.",
            "Handel detaliczny / e-commerce": "Niska marża, wysoka rotacja — ROE może być wysokie mimo niskich marż. Norma: 15–25%.",
        },
    },
    "roce": {
        "id": "roce",
        "nazwa": "Rentowność zaangażowanego kapitału (ROCE) %",
        "grupa": "Rentowność",
        "wzor": "Zysk operacyjny (EBIT) / (Aktywa razem − Zobowiązania krótkoterminowe) × 100",
        "wzor_latex": "ROCE = \\frac{EBIT}{Aktywa\\ razem - Zob.\\ krótk.} \\times 100",
        "opis": (
            "Lepsza miara niż ROE dla firm z różną strukturą finansowania. "
            "Uwzględnia zarówno dług długoterminowy, jak i kapitał własny. "
            "Szczególnie przydatny przy porównaniu firm z różnym poziomem dźwigni."
        ),
        "interpretacja_ogolna": "Powinien być wyższy niż WACC (koszt kapitału, zazwyczaj 8–12%). Dobry poziom: > 15%.",
        "uwagi_branzowe": {},
    },
    "gross_margin": {
        "id": "gross_margin",
        "nazwa": "Marża brutto na sprzedaży %",
        "grupa": "Rentowność",
        "wzor": "Zysk brutto ze sprzedaży / Przychody ze sprzedaży × 100",
        "wzor_latex": "GM = \\frac{Przychody - Koszt\\ własny\\ sprzedaży}{Przychody} \\times 100",
        "opis": (
            "Pokazuje, ile zostaje z każdej złotówki przychodu po odjęciu bezpośrednich "
            "kosztów wytworzenia/zakupu (bez kosztów ogólnych i sprzedaży). "
            "Odzwierciedla siłę cenową i efektywność produkcji."
        ),
        "interpretacja_ogolna": "Im wyższy tym lepiej. Branże: handel 20–35%, produkcja 30–50%, SaaS 60–80%.",
        "uwagi_branzowe": {
            "Usługi IT / SaaS": "Marża > 70% to norma dla dojrzałego SaaS. Poniżej 50% sugeruje zbyt wysokie koszty serwerowe lub wsparcia.",
            "Handel detaliczny / e-commerce": "Marże 15–30% są typowe. Zależy silnie od mixu produktowego.",
        },
    },
    "operating_margin": {
        "id": "operating_margin",
        "nazwa": "Marża operacyjna (EBIT Margin) %",
        "grupa": "Rentowność",
        "wzor": "Zysk operacyjny (EBIT) / Przychody ze sprzedaży × 100",
        "wzor_latex": "EBIT\\_Margin = \\frac{EBIT}{Przychody} \\times 100",
        "opis": (
            "Mierzy rentowność działalności operacyjnej — przed odsetkami i podatkami. "
            "Porównuje firmy niezależnie od struktury finansowania i jurysdykcji podatkowej. "
            "Jest podstawą wyceny metodą EV/EBIT."
        ),
        "interpretacja_ogolna": "Dobry poziom: > 10%. Branże: handel 3–7%, produkcja 8–15%, IT 20–35%.",
        "uwagi_branzowe": {
            "Usługi IT / SaaS": "Dojrzały SaaS: > 20%. Wzrostowy SaaS może być ujemny — ok, jeśli rośnie ARR.",
            "Energetyka i utilities": "Regulowane marże — norma 10–15%.",
        },
    },
    "ebitda_margin": {
        "id": "ebitda_margin",
        "nazwa": "Marża EBITDA %",
        "grupa": "Rentowność",
        "wzor": "(Zysk operacyjny + D&A) / Przychody × 100 ≈ (Zysk netto + Koszty fin. + Podatek) / Przychody × 100",
        "wzor_latex": "EBITDA\\_Margin = \\frac{Zysk\\ netto + Odsetki + Podatek + D\\&A}{Przychody} \\times 100",
        "opis": (
            "Przybliżenie cash flow operacyjnego — wyklucza amortyzację, odsetki i podatek. "
            "Używana przy wycenach transakcyjnych (EV/EBITDA). Uwaga: ignoruje nakłady "
            "kapitałowe (CAPEX) — wysoka EBITDA przy wysokim CAPEX może być myląca."
        ),
        "interpretacja_ogolna": "Branże kapitałochłonne: 20–35%. SaaS i usługi: 30–50%. Handel: 5–10%.",
        "uwagi_branzowe": {
            "Energetyka i utilities": "EBITDA > 30% to norma przy dużych aktywach trwałych.",
            "Hotelarstwo i gastronomia": "EBITDA Margin 15–25% — ale uwaga na duże koszty remontu (CAPEX).",
        },
    },
    "cost_income_ratio": {
        "id": "cost_income_ratio",
        "nazwa": "Wskaźnik kosztów do przychodów (CIR) %",
        "grupa": "Rentowność",
        "wzor": "Koszty operacyjne / Przychody ze sprzedaży × 100",
        "wzor_latex": "CIR = \\frac{Koszty\\ operacyjne}{Przychody} \\times 100",
        "opis": (
            "Im niższy tym lepiej — firma generuje przychody przy niższych kosztach. "
            "Szczególnie popularny w analizie banków i ubezpieczycieli. "
            "Spadający CIR w czasie = rosnąca efektywność operacyjna."
        ),
        "interpretacja_ogolna": "Dobry CIR: < 60%. Branże: banki < 55%, handel < 85% (niska marża), IT < 60%.",
        "uwagi_branzowe": {
            "Usługi finansowe / Ubezpieczenia": "Kluczowy KPI banków. < 50% to bardzo efektywny bank. > 70% to problem.",
        },
    },

    # ── ZADŁUŻENIE ───────────────────────────────────────────────────────────
    "debt_ratio": {
        "id": "debt_ratio",
        "nazwa": "Wskaźnik zadłużenia ogólnego (Debt Ratio) %",
        "grupa": "Zadłużenie",
        "wzor": "Zobowiązania ogółem / Aktywa razem × 100",
        "wzor_latex": "DR = \\frac{Zobowiązania\\ ogółem}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Pokazuje, jaka część majątku firmy finansowana jest długiem. "
            "To podstawowy miernik ryzyka finansowego z perspektywy wierzycieli. "
            "Wysoki DR = wysoka dźwignia = wyższe ryzyko bankructwa, ale też potencjalnie wyższy ROE."
        ),
        "interpretacja_ogolna": "Bezpieczny zakres: 40–60%. Powyżej 75% to strefa wysokiego ryzyka dla większości branż.",
        "uwagi_branzowe": {
            "Usługi finansowe / Ubezpieczenia": "Banki normalnie mają DR = 85–92% — depozyty to zobowiązania, ale jest to model regulowany.",
            "Usługi IT / SaaS": "Dobre firmy SaaS mają DR < 40%. Wysoki dług przy subskrypcyjnym modelu = alarm.",
            "Budownictwo i deweloperka": "Deweloperzy z DR = 65–80% to norma przy finansowaniu projektów kredytem.",
        },
    },
    "equity_ratio": {
        "id": "equity_ratio",
        "nazwa": "Wskaźnik samofinansowania (Equity Ratio) %",
        "grupa": "Zadłużenie",
        "wzor": "Kapitał własny / Aktywa razem × 100",
        "wzor_latex": "ER = \\frac{Kapitał\\ własny}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Dopełnienie Debt Ratio do 100%. Mówi, jaka część majątku jest finansowana "
            "kapitałem właścicieli. Im wyższy, tym firma jest bardziej odporna na "
            "kryzysy i ma większą zdolność do dalszego zadłużania się."
        ),
        "interpretacja_ogolna": "Bezpieczny poziom: > 40%. Poniżej 25% — firma silnie uzależniona od kredytodawców.",
        "uwagi_branzowe": {},
    },
    "dte": {
        "id": "dte",
        "nazwa": "Zadłużenie do kapitału własnego (D/E Ratio) %",
        "grupa": "Zadłużenie",
        "wzor": "Zobowiązania ogółem / Kapitał własny × 100",
        "wzor_latex": "D/E = \\frac{Zobowiązania\\ ogółem}{Kapitał\\ własny} \\times 100",
        "opis": (
            "Klasyczna miara dźwigni finansowej — ile długu przypada na każdą złotówkę "
            "kapitału własnego. D/E = 100% oznacza, że dług równa się kapitałowi własnemu. "
            "Inwestorzy wzrostowi tolerują wyższe D/E, wartościowi wolą niskie."
        ),
        "interpretacja_ogolna": "Norma: < 100%. Umiarkowane: 100–200%. Powyżej 300% — strefa ryzyka.",
        "uwagi_branzowe": {
            "Budownictwo i deweloperka": "D/E = 150–250% jest typowe przy projektach deweloperskich finansowanych kredytem.",
            "Usługi IT / SaaS": "D/E < 50% to dobry znak. Wysokie D/E przy niskich marżach = niebezpieczne.",
        },
    },
    "icr": {
        "id": "icr",
        "nazwa": "Wskaźnik pokrycia odsetek (ICR / Interest Coverage Ratio)",
        "grupa": "Zadłużenie",
        "wzor": "Zysk operacyjny (EBIT) / Koszty finansowe (odsetki)",
        "wzor_latex": "ICR = \\frac{EBIT}{Odsetki}",
        "opis": (
            "Kluczowy dla banków udzielających kredytów — pokazuje, ile razy EBIT pokrywa "
            "roczne odsetki. ICR = 1 oznacza, że cały zysk operacyjny idzie na odsetki. "
            "Poniżej 1.5 banki zazwyczaj odmawiają kredytu lub żądają dodatkowych zabezpieczeń."
        ),
        "interpretacja_ogolna": "Bardzo dobry: > 5. Dobry: 3–5. Wymaga uwagi: 1,5–3. Niebezpieczny: < 1,5.",
        "uwagi_branzowe": {
            "Energetyka i utilities": "Stabilne przepływy gotówki — ICR = 2,5 jest tu akceptowalny.",
            "Hotelarstwo i gastronomia": "Sezonowość — oceniaj ICR na danych rocznych, nie kwartalnych.",
        },
    },
    "debt_to_ebitda": {
        "id": "debt_to_ebitda",
        "nazwa": "Dług netto / EBITDA (przybliżony)",
        "grupa": "Zadłużenie",
        "wzor": "Zobowiązania ogółem / (Zysk netto + Koszty finansowe) [przybliżenie EBITDA]",
        "wzor_latex": "Net\\ Debt/EBITDA \\approx \\frac{Zob.\\ ogółem}{Zysk\\ netto + Koszty\\ fin.}",
        "opis": (
            "Odpowiada na pytanie: ile lat zajmie spłata całego długu z generowanej EBITDA? "
            "Używany przez banki do oceny zdolności kredytowej i przy przejęciach (M&A). "
            "Uwaga: to przybliżenie bez danych o amortyzacji z e-Sprawozdania."
        ),
        "interpretacja_ogolna": "Norma bankowa: < 3,5x. Przy LBO: do 6x. Powyżej 7x — zona niebezpieczeństwa.",
        "uwagi_branzowe": {
            "Energetyka i utilities": "Regulowane firmy utility: do 5x jest akceptowalne ze względu na stabilne cash flow.",
        },
    },
    "net_debt_ratio": {
        "id": "net_debt_ratio",
        "nazwa": "Wskaźnik zadłużenia netto (Net Debt Ratio) %",
        "grupa": "Zadłużenie",
        "wzor": "(Zobowiązania ogółem − Inwestycje krótkoterminowe) / Aktywa razem × 100",
        "wzor_latex": "NDR = \\frac{Zob.\\ ogółem - Gotówka}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Udoskonalona wersja Debt Ratio — odejmuje posiadaną gotówkę od długu, "
            "bo firma mogłaby teoretycznie go nią spłacić. Daje bardziej realistyczny "
            "obraz zadłużenia netto."
        ),
        "interpretacja_ogolna": "Powinien być niższy od Debt Ratio. Ujemny Net Debt Ratio oznacza, że firma ma więcej gotówki niż długów.",
        "uwagi_branzowe": {},
    },
    "lt_debt_ratio": {
        "id": "lt_debt_ratio",
        "nazwa": "Wskaźnik zadłużenia długoterminowego %",
        "grupa": "Zadłużenie",
        "wzor": "(Zobowiązania ogółem − Zobowiązania krótkoterminowe) / Aktywa razem × 100",
        "wzor_latex": "LT\\_DR = \\frac{Zob.\\ ogółem - Zob.\\ krótk.}{Aktywa\\ razem} \\times 100",
        "opis": (
            "Izoluje dług długoterminowy (kredyty, obligacje, leasing). "
            "Ważny przy analizie struktury finansowania inwestycji. "
            "Wysoki udział długoterminowego zadłużenia przy dobrym ICR nie jest problemem."
        ),
        "interpretacja_ogolna": "Zależy od branży. W produkcji i energetyce 30–50% jest normą.",
        "uwagi_branzowe": {},
    },

    # ── SPRAWNOŚĆ / EFEKTYWNOŚĆ ──────────────────────────────────────────────
    "ato": {
        "id": "ato",
        "nazwa": "Rotacja aktywów (Asset Turnover — ATO)",
        "grupa": "Sprawność",
        "wzor": "Przychody ze sprzedaży / Aktywa razem",
        "wzor_latex": "ATO = \\frac{Przychody}{Aktywa\\ razem}",
        "opis": (
            "Mierzy, ile złotych przychodu generuje każda złotówka majątku. "
            "Odzwierciedla intensywność wykorzystania aktywów. Niski ATO przy "
            "wysokich marżach (np. luksus) jest ok — wysoki ATO przy niskich marżach (handel) też.",
        ),
        "interpretacja_ogolna": "Handel: 1,5–3x. Produkcja: 0,8–1,5x. IT: 0,5–1,5x. Energetyka: 0,3–0,6x.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Wysoki ATO (> 1,5) to kluczowy driver rentowności przy niskich marżach.",
            "Energetyka i utilities": "Ogromne aktywa trwałe → ATO < 0,5 jest normą.",
        },
    },
    "fixed_asset_turnover": {
        "id": "fixed_asset_turnover",
        "nazwa": "Rotacja aktywów trwałych",
        "grupa": "Sprawność",
        "wzor": "Przychody / (Aktywa razem − Aktywa obrotowe)",
        "wzor_latex": "FAT = \\frac{Przychody}{Aktywa\\ trwałe}",
        "opis": (
            "Mierzy, jak efektywnie firma używa aktywów trwałych (maszyny, budynki, "
            "linie produkcyjne). Kluczowy w branżach kapitałochłonnych. "
            "Rosnący trend oznacza poprawę wykorzystania zdolności produkcyjnych."
        ),
        "interpretacja_ogolna": "Produkcja: > 2,0. Energetyka: 0,5–1,0. IT (data center): 1–2.",
        "uwagi_branzowe": {
            "Produkcja przemysłowa": "FAT < 1,0 sygnalizuje niedostateczne wykorzystanie mocy produkcyjnych.",
        },
    },
    "working_capital_turnover": {
        "id": "working_capital_turnover",
        "nazwa": "Rotacja kapitału obrotowego",
        "grupa": "Sprawność",
        "wzor": "Przychody / (Aktywa obrotowe − Zobowiązania krótkoterminowe)",
        "wzor_latex": "WCT = \\frac{Przychody}{Kapitał\\ obrotowy\\ netto}",
        "opis": (
            "Pokazuje, ile przychodów generuje każda złotówka kapitału obrotowego. "
            "Wysoki wskaźnik sugeruje efektywne zarządzanie cyklem operacyjnym. "
            "Negatywny kapitał obrotowy daje ujemny wynik — sygnał strukturalnego ryzyka."
        ),
        "interpretacja_ogolna": "Im wyższy tym lepiej, ale ujemny jest niepokojący. Norma: 3–8x w handlu.",
        "uwagi_branzowe": {},
    },
    "inventory_turnover_days": {
        "id": "inventory_turnover_days",
        "nazwa": "Rotacja zapasów (Inventory Turnover Days — DIO)",
        "grupa": "Sprawność",
        "wzor": "Zapasy / Koszty działalności operacyjnej × 365",
        "wzor_latex": "DIO = \\frac{Zapasy}{Koszty\\ operacyjne} \\times 365",
        "opis": (
            "Ile dni zajmuje firmie upłynnienie zapasów. Niskie DIO = szybka rotacja = "
            "mniejsze ryzyko przestarzałości i niższe koszty magazynowania. "
            "Zbyt niskie może też oznaczać brak buforu przy zakłóceniach dostaw."
        ),
        "interpretacja_ogolna": "Handel spożywczy: < 30 dni. Handel ogólny: 30–60 dni. Produkcja: 60–120 dni. Budownictwo: 90–180 dni.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "E-commerce celuje w < 30 dni. Wolna rotacja = zamrożony kapitał.",
            "Produkcja przemysłowa": "Produkcja pod zamówienie może mieć DIO > 90 dni — to norma.",
            "Rolnictwo i spożywczy": "Bardzo niskie DIO (< 20 dni) dla produktów świeżych. Wyższe dla zbóż i przetworów.",
        },
    },
    "receivables_turnover_days": {
        "id": "receivables_turnover_days",
        "nazwa": "Rotacja należności (Days Sales Outstanding — DSO)",
        "grupa": "Sprawność",
        "wzor": "Należności krótkoterminowe / Przychody ze sprzedaży × 365",
        "wzor_latex": "DSO = \\frac{Należności}{Przychody} \\times 365",
        "opis": (
            "Ile dni czeka firma na zapłatę od klientów. Wysokie DSO = firma udziela "
            "długich kredytów kupieckich lub ma problemy z windykacją. "
            "Zestawia się z warunkami płatności w branży."
        ),
        "interpretacja_ogolna": "Cel: < 45 dni. Norma: 30–60 dni. Powyżej 90 dni — ryzyko zatorów. Powyżej 120 dni — poważny problem.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Sprzedaż gotówkowa → DSO < 10 dni. Wysoki DSO to anomalia.",
            "Budownictwo i deweloperka": "DSO = 60–120 dni jest normą przy długich cyklach projektów.",
            "Usługi IT / SaaS": "Model subskrypcyjny → DSO < 30 dni. Wysoki DSO sugeruje ryzyko churn.",
        },
    },
    "payables_turnover_days": {
        "id": "payables_turnover_days",
        "nazwa": "Rotacja zobowiązań handlowych (Days Payable Outstanding — DPO)",
        "grupa": "Sprawność",
        "wzor": "Zobowiązania krótkoterminowe / Koszty działalności operacyjnej × 365",
        "wzor_latex": "DPO = \\frac{Zob.\\ krótk.}{Koszty\\ operacyjne} \\times 365",
        "opis": (
            "Ile dni firma zwleka z płatnością dostawcom. Wysokie DPO = firma używa "
            "dostawców jak darmowego banku (bezpłatny kredyt kupiecki). "
            "Zbyt wysokie może zaszkodzić relacjom z dostawcami i reputacji."
        ),
        "interpretacja_ogolna": "Norma: 30–60 dni. Duże sieci handlowe: 60–90 dni (siła przetargowa). Powyżej 120 dni — sygnał problemów płatniczych.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Duże sieci (np. hipermarkety) mają DPO > 60 dni — model oparty na sile przetargowej.",
        },
    },
    "cash_conversion_cycle": {
        "id": "cash_conversion_cycle",
        "nazwa": "Cykl konwersji gotówki (Cash Conversion Cycle — CCC) dni",
        "grupa": "Sprawność",
        "wzor": "DIO + DSO − DPO",
        "wzor_latex": "CCC = DIO + DSO - DPO",
        "opis": (
            "Ile dni gotówka firmy jest 'uwięziona' w cyklu operacyjnym. "
            "CCC = 0 oznacza, że firma nie potrzebuje kapitału obrotowego. "
            "Ujemne CCC (jak Amazon, Żabka) = firma dostaje pieniądze zanim zapłaci dostawcom. "
            "To przewaga konkurencyjna o ogromnej wartości."
        ),
        "interpretacja_ogolna": "Im niższy tym lepiej. Ujemny CCC to święty Graal efektywności operacyjnej.",
        "uwagi_branzowe": {
            "Handel detaliczny / e-commerce": "Wielkie sieci dążą do ujemnego CCC. E-commerce typowo 20–40 dni.",
            "Produkcja przemysłowa": "CCC = 60–120 dni jest typowe. Kluczowe jest zarządzanie zapasami.",
        },
    },

    # ── STRUKTURA KAPITAŁOWA ─────────────────────────────────────────────────
    "golden_rule": {
        "id": "golden_rule",
        "nazwa": "Złota reguła bilansowa (%)",
        "grupa": "Struktura kapitałowa",
        "wzor": "Kapitał własny / (Aktywa razem − Aktywa obrotowe) × 100",
        "wzor_latex": "ZRB = \\frac{Kapitał\\ własny}{Aktywa\\ trwałe} \\times 100",
        "opis": (
            "Klasyczna zasada finansów: aktywa trwałe powinny być finansowane "
            "kapitałem stałym (własnym lub długoterminowym). ZRB > 100% = złota reguła "
            "jest zachowana. ZRB < 100% = część aktywów trwałych finansowana krótkoterminowo — ryzykowne."
        ),
        "interpretacja_ogolna": "Powinna wynosić ≥ 100%. Poniżej 80% to sygnał strukturalnego ryzyka.",
        "uwagi_branzowe": {},
    },
    "silver_rule": {
        "id": "silver_rule",
        "nazwa": "Srebrna reguła bilansowa (%)",
        "grupa": "Struktura kapitałowa",
        "wzor": "(Kapitał własny + Zobowiązania długoterminowe) / (Aktywa razem − Aktywa obrotowe) × 100",
        "wzor_latex": "ZRS = \\frac{Kap.\\ stały}{Aktywa\\ trwałe} \\times 100",
        "opis": (
            "Łagodniejsza wersja złotej reguły — dopuszcza finansowanie aktywów trwałych "
            "kapitałem długoterminowym (kredyty inwestycyjne, obligacje). "
            "Powinna wynosić ≥ 100%, żeby cykl operacyjny był stabilny."
        ),
        "interpretacja_ogolna": "≥ 100% to minimum bezpieczeństwa. Poniżej 90% — firma finansuje aktywa trwałe zobowiązaniami bieżącymi.",
        "uwagi_branzowe": {},
    },
    "leverage_ratio": {
        "id": "leverage_ratio",
        "nazwa": "Mnożnik kapitałowy (Equity Multiplier / Dźwignia finansowa)",
        "grupa": "Struktura kapitałowa",
        "wzor": "Aktywa razem / Kapitał własny",
        "wzor_latex": "EM = \\frac{Aktywa\\ razem}{Kapitał\\ własny}",
        "opis": (
            "Komponent modelu DuPont — pokazuje, ile razy aktywa przewyższają kapitał własny. "
            "EM = 1 = firma bez długu. EM = 2 = dług równa się kapitałowi własnemu. "
            "Rozkład ROE wg DuPont: ROE = ROS × ATO × EM."
        ),
        "interpretacja_ogolna": "Norma: 1,5–3x. Banki: 8–15x (regulowane). Powyżej 5x w sektorze niefinansowym — wysokie ryzyko.",
        "uwagi_branzowe": {},
    },
    "dupont_roe": {
        "id": "dupont_roe",
        "nazwa": "DuPont — ROE rozłożony (veryfikacja)",
        "grupa": "Struktura kapitałowa",
        "wzor": "ROS × ATO × Mnożnik kapitałowy (powinien = ROE)",
        "wzor_latex": "ROE = \\frac{Zysk\\ netto}{Przychody} \\times \\frac{Przychody}{Aktywa} \\times \\frac{Aktywa}{Kap.\\ własny}",
        "opis": (
            "Klasyczna analiza DuPont rozkłada ROE na trzy czynniki: "
            "(1) Marżę — efektywność cenowa, "
            "(2) Rotację aktywów — efektywność operacyjna, "
            "(3) Dźwignię finansową — ryzyko struktury kapitału. "
            "Wartość powinna być bliska ROE — różnica > 1 pp. sygnalizuje błąd danych."
        ),
        "interpretacja_ogolna": "Analiza jakości ROE: wzrost ROE przez marżę lub rotację = zdrowy. Wzrost przez dźwignię = ryzykowny.",
        "uwagi_branzowe": {},
    },

    # ── RYZYKO I WIARYGODNOŚĆ ────────────────────────────────────────────────
    "altman_z_score": {
        "id": "altman_z_score",
        "nazwa": "Altman Z-Score (ryzyko bankructwa)",
        "grupa": "Ryzyko",
        "wzor": "1.2×X1 + 1.4×X2 + 3.3×X3 + 0.6×X4 + 1.0×X5",
        "wzor_latex": "Z = 1.2X_1 + 1.4X_2 + 3.3X_3 + 0.6X_4 + X_5",
        "opis": (
            "Model predykcji bankructwa Edwarda Altmana (1968) — jeden z najszerzej "
            "stosowanych modeli w analizie ryzyka. Pięć wskaźników: "
            "X1 = Kapitał obrotowy/Aktywa, X2 = Zyski zatrzymane/Aktywa (≈ KW/Aktywa), "
            "X3 = EBIT/Aktywa, X4 = KW/Zobowiązania, X5 = Przychody/Aktywa."
        ),
        "interpretacja_ogolna": "Z > 2,99 = bezpieczna strefa (zielona). Z = 1,81–2,99 = szara strefa (uwaga). Z < 1,81 = strefa bankructwa (czerwona).",
        "uwagi_branzowe": {
            "Usługi finansowe / Ubezpieczenia": "Model Altmana nie jest kalibrowany dla banków i ubezpieczycieli — użyj specjalistycznych modeli (Merton, CAMELS).",
            "Usługi IT / SaaS": "Młode firmy SaaS często mają niski Z-Score mimo dobrego modelu biznesowego — interpretuj ostrożnie.",
        },
    },
    "springate_score": {
        "id": "springate_score",
        "nazwa": "Springate S-Score (ryzyko bankructwa — alternatywa)",
        "grupa": "Ryzyko",
        "wzor": "1.03×A + 3.07×B + 0.66×C + 0.4×D",
        "wzor_latex": "S = 1.03A + 3.07B + 0.66C + 0.4D",
        "opis": (
            "Model Springate'a z 1978 — alternatywa dla Altmana, kalibrowana na "
            "kanadyjskich firmach. A = KO/Aktywa, B = EBIT/Aktywa, "
            "C = EBT/Zobowiązania bieżące, D = Przychody/Aktywa."
        ),
        "interpretacja_ogolna": "S > 0,862 = firma bezpieczna. S < 0,862 = potencjalne trudności finansowe.",
        "uwagi_branzowe": {},
    },
    "interest_bearing_debt": {
        "id": "interest_bearing_debt",
        "nazwa": "Udział oprocentowanego zadłużenia w aktywach %",
        "grupa": "Ryzyko",
        "wzor": "Koszty finansowe × (1/avg_stopa%) / Aktywa × 100 [przybliżenie]",
        "wzor_latex": "IBD\\_proxy = \\frac{Koszty\\ finansowe \\times 1/r}{Aktywa} \\times 100",
        "opis": (
            "Przybliżenie udziału oprocentowanego zadłużenia. Prawdziwa wartość wymaga "
            "noty do sprawozdania. Koszty finansowe odzwierciedlają wielkość długu "
            "oprocentowanego (kredyty bankowe, obligacje, leasing finansowy)."
        ),
        "interpretacja_ogolna": "Wskaźnik pomocniczy. Sprawdź noty do sprawozdania dla dokładnych danych.",
        "uwagi_branzowe": {},
    },

    # ── WARTOŚĆ DLA WŁAŚCICIELI ──────────────────────────────────────────────
    "book_value_per_equity": {
        "id": "book_value_per_equity",
        "nazwa": "Wartość księgowa kapitału własnego (PLN)",
        "grupa": "Wartość",
        "wzor": "Kapitał własny (wartość nominalna)",
        "wzor_latex": "BV = Kapitał\\ własny",
        "opis": (
            "Wartość netto firmy według ksiąg rachunkowych. To kwota, którą właściciele "
            "otrzymaliby teoretycznie po spłacie wszystkich długów. Rynkowa wycena "
            "(goodwill, marka, patenty) może znacznie przekraczać wartość księgową."
        ),
        "interpretacja_ogolna": "Wartość absolutna — ważna w porównaniu z wyceną rynkową (jeśli firma jest notowana).",
        "uwagi_branzowe": {},
    },
    "revenue_per_asset": {
        "id": "revenue_per_asset",
        "nazwa": "Przychód na jednostkę aktywów (PLN/PLN)",
        "grupa": "Wartość",
        "wzor": "Przychody ze sprzedaży / Aktywa razem",
        "wzor_latex": "RPA = \\frac{Przychody}{Aktywa}",
        "opis": "Tożsamy z ATO — ile przychodów generuje każda złotówka majątku. Patrz: Rotacja aktywów.",
        "interpretacja_ogolna": "Im wyższy tym lepiej. Zależy silnie od branży.",
        "uwagi_branzowe": {},
    },
    "profit_per_revenue_unit": {
        "id": "profit_per_revenue_unit",
        "nazwa": "Zysk na każde 100 zł przychodu (PLN)",
        "grupa": "Wartość",
        "wzor": "Zysk netto / Przychody × 100",
        "wzor_latex": "PPR = \\frac{Zysk\\ netto}{Przychody} \\times 100",
        "opis": (
            "Intuicyjna wersja ROS — ile złotych czystego zysku pozostaje po sprzedaży "
            "za 100 zł. Łatwa do komunikacji zarządowi i właścicielom nieprzyzwyczajonym "
            "do analizy finansowej."
        ),
        "interpretacja_ogolna": "Równe ROS w % — np. ROS = 5% → firma zarabia 5 zł na każdych 100 zł przychodu.",
        "uwagi_branzowe": {},
    },
    "equity_growth_proxy": {
        "id": "equity_growth_proxy",
        "nazwa": "Stopa retencji zysku (przybliżona) %",
        "grupa": "Wartość",
        "wzor": "Zysk netto / Kapitał własny × 100 [jako proxy wewnętrznej stopy wzrostu]",
        "wzor_latex": "IGR \\approx ROE \\times Retencja",
        "opis": (
            "Jeśli firma nie wypłaca dywidend, cały ROE staje się wewnętrzną stopą wzrostu "
            "kapitału własnego. Pokazuje, o ile procent rocznie rośnie wartość księgowa "
            "firmy wyłącznie przez zatrzymywanie zysku."
        ),
        "interpretacja_ogolna": "Im wyższy tym lepiej. 10–20% rocznie = solidny wzrost. > 20% = dynamiczny wzrost organiczny.",
        "uwagi_branzowe": {},
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# 2. GRUPY WSKAŹNIKÓW DO WYBORU W UI
# ═══════════════════════════════════════════════════════════════════════════════

GRUPY_WSKAZNIKOW = {
    "Płynność":              ["current_ratio", "quick_ratio", "cash_ratio", "nwc_ratio", "defensive_interval"],
    "Rentowność":            ["ros", "roa", "roe", "roce", "gross_margin", "operating_margin", "ebitda_margin", "cost_income_ratio"],
    "Zadłużenie":            ["debt_ratio", "equity_ratio", "dte", "icr", "debt_to_ebitda", "net_debt_ratio", "lt_debt_ratio"],
    "Sprawność":             ["ato", "fixed_asset_turnover", "working_capital_turnover",
                              "inventory_turnover_days", "receivables_turnover_days",
                              "payables_turnover_days", "cash_conversion_cycle"],
    "Struktura kapitałowa":  ["golden_rule", "silver_rule", "leverage_ratio", "dupont_roe"],
    "Ryzyko":                ["altman_z_score", "springate_score", "interest_bearing_debt"],
    "Wartość":               ["book_value_per_equity", "revenue_per_asset", "profit_per_revenue_unit", "equity_growth_proxy"],
}

# ═══════════════════════════════════════════════════════════════════════════════
# 3. SILNIK PARSOWANIA (bez zmian w logice — tylko czysty kod)
# ═══════════════════════════════════════════════════════════════════════════════

def parse_financial_hybrid(file_bytes, filename):
    content  = file_bytes.decode('utf-8', errors='ignore')
    soup_xml = BeautifulSoup(content, 'xml')

    def get_vals(parent, tag_names):
        if not parent:
            return 0.0
        for name in tag_names:
            tag = parent.find(name=re.compile(rf'^(.*:)?{name}$', re.I))
            if tag:
                kwota = tag.find(name=re.compile(r'^(.*:)?KwotaA$', re.I))
                if kwota and kwota.text:
                    try:
                        return float(kwota.text.strip())
                    except Exception:
                        pass
        return 0.0

    bilans = soup_xml.find(name=re.compile(r'^(.*:)?Bilans.*$', re.I))
    rzis   = soup_xml.find(name=re.compile(r'^(.*:)?RZiS.*$',   re.I))
    aktywa = get_vals(bilans, ['AktywaRazem', 'SumaAktywow', 'Aktywa'])

    if aktywa > 0:
        akt_obr    = get_vals(bilans, ['AktywaObrotowe', 'Aktywa_B'])
        zapasy     = get_vals(bilans, ['Zapasy', 'Aktywa_B_I'])
        nal_krotko = get_vals(bilans, ['NaleznosciKrotkoterminowe', 'Aktywa_B_II'])
        inw_krotko = get_vals(bilans, ['InwestycjeKrotkoterminowe', 'Aktywa_B_III'])
        kap_wlasny = get_vals(bilans, ['KapitalFunduszWlasny', 'KapitalWlasny', 'Pasywa_A'])
        zob_ogolem = get_vals(bilans, ['ZobowiazaniaIRezerwyNaZobowiazania', 'ZobowiazaniaOgolem', 'Pasywa_B'])
        zob_krotko = get_vals(bilans, ['ZobowiazaniaKrotkoterminowe', 'Pasywa_B_III'])
        przych     = get_vals(rzis,   ['PrzychodyNettoZeSprzedazy', 'PrzychodyNetto'])
        zysk_op    = get_vals(rzis,   ['ZyskStrataZDzialalnosciOperacyjnej'])
        zysk_br    = get_vals(rzis,   ['ZyskStrataBrutto'])
        zysk_nt    = get_vals(rzis,   ['ZyskStrataNetto'])
        koszty_fin = get_vals(rzis,   ['KosztyFinansowe'])
        koszty_op  = get_vals(rzis,   ['KosztyDzialalnosciOperacyjnej'])
        if koszty_op == 0.0:
            koszty_op = (
                get_vals(rzis, ['KosztWytworzeniaSprzedanychProduktow'])
                + get_vals(rzis, ['KosztySprzedazy'])
                + get_vals(rzis, ['KosztyOgolnegoZarzadu'])
            )

        rzis_kalk  = rzis.find(name=re.compile(r'^(.*:)?RZiSKalk$',  re.I)) if rzis else None
        rzis_porow = rzis.find(name=re.compile(r'^(.*:)?RZiSPorown$', re.I)) if rzis else None

        for src in [rzis_kalk, rzis_porow]:
            if not src:
                continue
            if przych    == 0: przych    = get_vals(src, ['A'])
            if koszty_op == 0: koszty_op = get_vals(src, ['B'])
            if zysk_op   == 0: zysk_op   = get_vals(src, ['E', 'F'])
            if koszty_fin== 0: koszty_fin= get_vals(src, ['I', 'H'])
            if zysk_br   == 0: zysk_br   = get_vals(src, ['J', 'I'])
            if zysk_nt   == 0: zysk_nt   = get_vals(src, ['L'])
            break

        if rzis and przych == 0:
            przych    = get_vals(rzis, ['A'])
            koszty_op = koszty_op or get_vals(rzis, ['B'])
            zysk_op   = zysk_op   or (przych - koszty_op)
            zysk_nt   = zysk_nt   or get_vals(rzis, ['F'])
            zysk_br   = zysk_br   or zysk_nt

        return {
            "AktywaRazem":                aktywa,
            "AktywaObrotowe":             akt_obr,
            "Zapasy":                     zapasy,
            "NaleznosciKrotkoterminowe":  nal_krotko,
            "InwestycjeKrotkoterminowe":  inw_krotko,
            "KapitalWlasny":              kap_wlasny,
            "ZobowiazaniaOgolem":         zob_ogolem,
            "ZobowiazaniaKrotkoterminowe":zob_krotko,
            "PrzychodySprzedaz":          przych,
            "ZyskOperacyjny":             zysk_op,
            "ZyskBrutto":                 zysk_br,
            "ZyskNetto":                  zysk_nt,
            "KosztyDzialalnosciOperacyjnej": koszty_op,
            "KosztyFinansowe":            koszty_fin,
        }, "XML/XAdES (Oficjalny algorytm KRS)"

    # HTML fallback
    soup_html = BeautifulSoup(content, 'lxml')
    def get_html_value(keywords):
        for kw in keywords:
            for elem in soup_html.find_all(string=re.compile(kw, re.I)):
                parent = elem.find_parent(['td', 'th', 'div', 'span'])
                if parent:
                    for sib in parent.find_next_siblings(['td', 'th', 'div']):
                        txt = sib.get_text(strip=True).replace('\xa0','').replace(' ','').replace(',','.')
                        if re.match(r'^-?\d+(\.\d+)?$', txt):
                            try:   return float(txt)
                            except: pass
        return 0.0

    data_html = {
        "AktywaRazem":                 get_html_value(["Aktywa razem", "Suma aktywów"]),
        "AktywaObrotowe":              get_html_value(["Aktywa obrotowe"]),
        "Zapasy":                      get_html_value(["Zapasy"]),
        "NaleznosciKrotkoterminowe":   get_html_value(["Należności krótkoterminowe"]),
        "InwestycjeKrotkoterminowe":   get_html_value(["Inwestycje krótkoterminowe"]),
        "KapitalWlasny":               get_html_value(["Kapitał własny", "Kapitał (fundusz) własny"]),
        "ZobowiazaniaOgolem":          get_html_value(["Zobowiązania i rezerwy", "Zobowiązania ogółem"]),
        "ZobowiazaniaKrotkoterminowe": get_html_value(["Zobowiązania krótkoterminowe"]),
        "PrzychodySprzedaz":           get_html_value(["Przychody netto ze sprzedaży", "Przychody netto"]),
        "ZyskOperacyjny":              get_html_value(["Zysk (strata) z działalności operacyjnej"]),
        "ZyskBrutto":                  get_html_value(["Zysk (strata) brutto"]),
        "ZyskNetto":                   get_html_value(["Zysk (strata) netto"]),
        "KosztyDzialalnosciOperacyjnej": get_html_value(["Koszty działalności operacyjnej"]),
        "KosztyFinansowe":             get_html_value(["Koszty finansowe"]),
    }
    if data_html["AktywaRazem"] > 0:
        return data_html, "HTML (Ekstrakcja ze struktury wizualnej)"
    return None, "Nie rozpoznano struktury finansowej"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SILNIK OBLICZENIOWY — każdy wskaźnik jako osobna funkcja
# ═══════════════════════════════════════════════════════════════════════════════

def _sd(n, d):
    """Bezpieczne dzielenie — zwraca 0.0 jeśli dzielnik = 0."""
    return n / d if d and d != 0 else 0.0

def _interp(value, wskaznik_id, branza):
    """Zwraca krotkę (ikona, opis) na podstawie progów branżowych."""
    progi = PROGI_BRANZOWE.get(wskaznik_id, {})
    if not progi:
        return "⚪", "Brak progów branżowych"
    p = progi.get(branza) or progi.get("Ogólna (domyślna)")
    if not p:
        return "⚪", "Brak danych dla branży"
    lo_ok, hi_ok, lo_warn, hi_warn = p
    if lo_ok <= value <= hi_ok:
        return "🟢", "Optymalna"
    elif lo_warn <= value <= hi_warn:
        return "🟡", "Wymaga uwagi"
    else:
        return "🔴", "Poza normą"

def compute_all_ratios(data: dict, branza: str, wybrane_id: list) -> list:
    """Oblicza wszystkie wybrane wskaźniki i zwraca listę słowników."""
    d   = data
    kop = d['KosztyDzialalnosciOperacyjnej'] if d['KosztyDzialalnosciOperacyjnej'] > 0 else d['PrzychodySprzedaz']
    akt_trwale = max(d['AktywaRazem'] - d['AktywaObrotowe'], 0.01)
    kap_obr    = d['AktywaObrotowe'] - d['ZobowiazaniaKrotkoterminowe']
    kap_staly  = d['KapitalWlasny'] + (d['ZobowiazaniaOgolem'] - d['ZobowiazaniaKrotkoterminowe'])

    # Pre-computed building blocks
    ebit  = d['ZyskOperacyjny']
    ebitda_proxy = d['ZyskNetto'] + d['KosztyFinansowe']  # bez amortyzacji z e-Sprawozdania

    # --- słownik kalkulatorów ---
    calc = {
        # PŁYNNOŚĆ
        "current_ratio":      lambda: _sd(d['AktywaObrotowe'], d['ZobowiazaniaKrotkoterminowe']),
        "quick_ratio":        lambda: _sd(d['AktywaObrotowe'] - d['Zapasy'], d['ZobowiazaniaKrotkoterminowe']),
        "cash_ratio":         lambda: _sd(d['InwestycjeKrotkoterminowe'], d['ZobowiazaniaKrotkoterminowe']),
        "nwc_ratio":          lambda: _sd(kap_obr, d['AktywaRazem']) * 100,
        "defensive_interval": lambda: _sd(d['InwestycjeKrotkoterminowe'] + d['NaleznosciKrotkoterminowe'], kop / 365),

        # RENTOWNOŚĆ
        "ros":             lambda: _sd(d['ZyskNetto'],   d['PrzychodySprzedaz']) * 100,
        "roa":             lambda: _sd(d['ZyskNetto'],   d['AktywaRazem'])       * 100,
        "roe":             lambda: _sd(d['ZyskNetto'],   d['KapitalWlasny'])     * 100,
        "roce":            lambda: _sd(ebit, d['AktywaRazem'] - d['ZobowiazaniaKrotkoterminowe']) * 100,
        "gross_margin":    lambda: _sd(d['ZyskBrutto'],  d['PrzychodySprzedaz']) * 100,
        "operating_margin":lambda: _sd(ebit,             d['PrzychodySprzedaz']) * 100,
        "ebitda_margin":   lambda: _sd(ebitda_proxy,     d['PrzychodySprzedaz']) * 100,
        "cost_income_ratio":lambda: _sd(kop,             d['PrzychodySprzedaz']) * 100,

        # ZADŁUŻENIE
        "debt_ratio":      lambda: _sd(d['ZobowiazaniaOgolem'],            d['AktywaRazem'])   * 100,
        "equity_ratio":    lambda: _sd(d['KapitalWlasny'],                 d['AktywaRazem'])   * 100,
        "dte":             lambda: _sd(d['ZobowiazaniaOgolem'],            d['KapitalWlasny']) * 100,
        "icr":             lambda: _sd(ebit,                               d['KosztyFinansowe']),
        "debt_to_ebitda":  lambda: _sd(d['ZobowiazaniaOgolem'],            max(ebitda_proxy, 0.01)),
        "net_debt_ratio":  lambda: _sd(d['ZobowiazaniaOgolem'] - d['InwestycjeKrotkoterminowe'], d['AktywaRazem']) * 100,
        "lt_debt_ratio":   lambda: _sd(d['ZobowiazaniaOgolem'] - d['ZobowiazaniaKrotkoterminowe'], d['AktywaRazem']) * 100,

        # SPRAWNOŚĆ
        "ato":                       lambda: _sd(d['PrzychodySprzedaz'], d['AktywaRazem']),
        "fixed_asset_turnover":      lambda: _sd(d['PrzychodySprzedaz'], akt_trwale),
        "working_capital_turnover":  lambda: _sd(d['PrzychodySprzedaz'], kap_obr) if kap_obr > 0 else 0.0,
        "inventory_turnover_days":   lambda: _sd(d['Zapasy'], kop) * 365,
        "receivables_turnover_days": lambda: _sd(d['NaleznosciKrotkoterminowe'], d['PrzychodySprzedaz']) * 365,
        "payables_turnover_days":    lambda: _sd(d['ZobowiazaniaKrotkoterminowe'], kop) * 365,
        "cash_conversion_cycle":     lambda: (
            _sd(d['Zapasy'], kop) * 365
            + _sd(d['NaleznosciKrotkoterminowe'], d['PrzychodySprzedaz']) * 365
            - _sd(d['ZobowiazaniaKrotkoterminowe'], kop) * 365
        ),

        # STRUKTURA KAPITAŁOWA
        "golden_rule":   lambda: _sd(d['KapitalWlasny'], akt_trwale) * 100,
        "silver_rule":   lambda: _sd(kap_staly, akt_trwale) * 100,
        "leverage_ratio":lambda: _sd(d['AktywaRazem'], d['KapitalWlasny']),
        "dupont_roe":    lambda: (
            _sd(d['ZyskNetto'], d['PrzychodySprzedaz'])
            * _sd(d['PrzychodySprzedaz'], d['AktywaRazem'])
            * _sd(d['AktywaRazem'], d['KapitalWlasny'])
        ) * 100,

        # RYZYKO
        "altman_z_score": lambda: (
            1.2 * _sd(kap_obr, d['AktywaRazem'])
            + 1.4 * _sd(d['KapitalWlasny'], d['AktywaRazem'])
            + 3.3 * _sd(ebit, d['AktywaRazem'])
            + 0.6 * _sd(d['KapitalWlasny'], d['ZobowiazaniaOgolem'])
            + 1.0 * _sd(d['PrzychodySprzedaz'], d['AktywaRazem'])
        ),
        "springate_score": lambda: (
            1.03 * _sd(kap_obr, d['AktywaRazem'])
            + 3.07 * _sd(ebit, d['AktywaRazem'])
            + 0.66 * _sd(d['ZyskBrutto'], d['ZobowiazaniaKrotkoterminowe'])
            + 0.4  * _sd(d['PrzychodySprzedaz'], d['AktywaRazem'])
        ),
        "interest_bearing_debt": lambda: _sd(d['KosztyFinansowe'], d['AktywaRazem']) * 100,

        # WARTOŚĆ
        "book_value_per_equity":   lambda: d['KapitalWlasny'],
        "revenue_per_asset":       lambda: _sd(d['PrzychodySprzedaz'], d['AktywaRazem']),
        "profit_per_revenue_unit": lambda: _sd(d['ZyskNetto'], d['PrzychodySprzedaz']) * 100,
        "equity_growth_proxy":     lambda: _sd(d['ZyskNetto'], d['KapitalWlasny']) * 100,
    }

    # Specjalna interpretacja dla wskaźników bez standardowych progów
    SPECIAL_INTERP = {
        "altman_z_score": lambda v: ("🟢","Bezpieczna strefa (Z > 2,99)") if v > 2.99
                          else (("🟡","Szara strefa (1,81–2,99)") if v >= 1.81 else ("🔴","Strefa bankructwa (Z < 1,81)")),
        "springate_score":lambda v: ("🟢","Firma bezpieczna (S > 0,862)") if v > 0.862 else ("🔴","Potencjalne trudności (S < 0,862)"),
        "golden_rule":    lambda v: ("🟢","Złota reguła zachowana") if v >= 100 else ("🔴","Złota reguła naruszona"),
        "silver_rule":    lambda v: ("🟢","Srebrna reguła zachowana") if v >= 100 else ("🟡","Srebrna reguła naruszona"),
        "nwc_ratio":      lambda v: ("🟢","Dodatni kapitał obrotowy") if v > 5 else ("🟡","Niski/ujemny kapitał obrotowy") if v >= 0 else ("🔴","Ujemny kapitał obrotowy"),
        "cash_conversion_cycle": lambda v: ("🟢","Ujemny CCC — wzorcowy") if v < 0 else ("🟢","Krótki cykl") if v < 30 else ("🟡","Przeciętny cykl") if v < 60 else ("🔴","Długi cykl — zamrożony kapitał"),
        "defensive_interval": lambda v: ("🟢","Bezpieczny runway > 60 dni") if v > 60 else ("🟡","Runway 30–60 dni") if v >= 30 else ("🔴","Krytyczny runway < 30 dni"),
        "icr":            lambda v: ("🟢","Bezpieczne > 3x") if v >= 3 else ("🟡","Wymaga uwagi 1,5–3x") if v >= 1.5 else ("🔴","Zagrożona obsługa odsetek"),
        "cost_income_ratio": lambda v: ("🟢","Efektywne < 60%") if v < 60 else ("🟡","Przeciętne 60–80%") if v < 80 else ("🔴","Kosztochłonne > 80%"),
        "dupont_roe":     lambda v: ("🟢","Weryfikacja DuPont OK") if v > 0 else ("🔴","Ujemne ROE"),
        "interest_bearing_debt": lambda v: ("⚪","Wskaźnik informacyjny"),
        "book_value_per_equity": lambda v: ("🟢","Kapitał własny > 0") if v > 0 else ("🔴","Ujemny kapitał własny — techniczna upadłość"),
        "revenue_per_asset": lambda v: ("⚪","Zależy od branży — patrz ATO"),
        "profit_per_revenue_unit": lambda v: ("🟢","Zysk > 0") if v > 0 else ("🔴","Strata"),
        "equity_growth_proxy": lambda v: ("🟢",f"Wzrost KW o ~{v:.1f}% rocznie") if v > 0 else ("🔴","Spadek kapitału własnego"),
        "lt_debt_ratio":  lambda v: ("⚪","Wskaźnik informacyjny — ocena zależy od branży"),
        "net_debt_ratio": lambda v: ("🟢","Dług netto ujemny — gotówka > dług") if v < 0 else ("🟡","Umiarkowany dług netto") if v < 40 else ("🔴","Wysokie zadłużenie netto"),
    }

    wyniki = []
    for wid in wybrane_id:
        if wid not in calc:
            continue
        defn  = WSKAZNIKI_DEFINICJE[wid]
        try:
            value = round(calc[wid](), 4)
        except Exception:
            value = 0.0

        if wid in SPECIAL_INTERP:
            ikona, opis_interp = SPECIAL_INTERP[wid](value)
        elif wid in PROGI_BRANZOWE:
            ikona, opis_interp = _interp(value, wid, branza)
        else:
            ikona, opis_interp = "⚪", "—"

        wyniki.append({
            "Grupa":         defn["grupa"],
            "Wskaźnik":      defn["nazwa"],
            "Wynik":         value,
            "Status":        f"{ikona} {opis_interp}",
            "_id":           wid,
        })
    return wyniki


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GŁÓWNY MODUŁ STREAMLIT
# ═══════════════════════════════════════════════════════════════════════════════

def _render_encyclopedia(wybrane_id: list, branza: str):
    """Renderuje encyklopedię wybranych wskaźników."""
    st.markdown("## 📚 Encyklopedia wybranych wskaźników")
    for wid in wybrane_id:
        defn = WSKAZNIKI_DEFINICJE.get(wid)
        if not defn:
            continue
        with st.expander(f"**{defn['grupa']}** › {defn['nazwa']}", expanded=False):
            col_l, col_r = st.columns([1, 1])
            with col_l:
                st.markdown("**📐 Wzór:**")
                st.code(defn["wzor"], language=None)
                st.markdown("**📖 Definicja:**")
                st.write(defn["opis"])
            with col_r:
                st.markdown("**📊 Interpretacja ogólna:**")
                st.info(defn["interpretacja_ogolna"])
                uwaga = defn.get("uwagi_branzowe", {}).get(branza)
                if uwaga:
                    st.markdown(f"**🏭 Uwaga branżowa ({branza}):**")
                    st.success(uwaga)
                elif defn.get("uwagi_branzowe"):
                    st.markdown("**🏭 Uwagi branżowe (inne branże):**")
                    for br, txt in defn["uwagi_branzowe"].items():
                        st.markdown(f"- **{br}:** {txt}")


def _render_excel(dane_finansowe, df_wyniki, df_raw):
    """Generuje plik Excel z audytem."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_wyniki.drop(columns=["_id"], errors='ignore').to_excel(
            writer, sheet_name='Analiza Wskaźnikowa', index=False
        )
        df_raw.to_excel(writer, sheet_name='Dane Źródłowe', index=False)

        wb  = writer.book
        fmt_hdr  = wb.add_format({'bg_color': '#2C3E50', 'font_color': '#FFFFFF', 'bold': True, 'border': 1})
        fmt_ziel = wb.add_format({'bg_color': '#D5F5E3', 'border': 1})
        fmt_zolt = wb.add_format({'bg_color': '#FEF9E7', 'border': 1})
        fmt_czer = wb.add_format({'bg_color': '#FADBD8', 'border': 1})

        ws = writer.sheets['Analiza Wskaźnikowa']
        cols = df_wyniki.drop(columns=["_id"], errors='ignore').columns
        for col_num, col_val in enumerate(cols):
            ws.write(0, col_num, col_val, fmt_hdr)
        ws.set_column('A:A', 18)
        ws.set_column('B:B', 45)
        ws.set_column('C:C', 14)
        ws.set_column('D:D', 35)

        for row_num, status in enumerate(df_wyniki["Status"], start=1):
            fmt = fmt_ziel if "🟢" in str(status) else (fmt_zolt if "🟡" in str(status) else fmt_czer)
            for col_num in range(len(cols)):
                ws.set_row(row_num, None, fmt)

        ws2 = writer.sheets['Dane Źródłowe']
        ws2.set_column('A:A', 35)
        ws2.set_column('B:B', 20)

    return output.getvalue()


def run_module():
    st.title("📈 Analiza Wskaźnikowa CFO — DorAIdca Radar v3")
    st.markdown(
        "Automatyczny audyt kondycji finansowej z e-Sprawozdań KRS. "
        "Wybierz branżę, wskaźniki i załaduj plik XML/XAdES/HTML."
    )

    # ── PANEL KONFIGURACJI ─────────────────────────────────────────────────
    with st.container():
        col_b, col_f = st.columns([1, 2])
        with col_b:
            branza = st.selectbox("🏭 Branża / sektor:", BRANZE, index=0,
                                   help="Branżowe progi zmieniają interpretację wskaźników.")
        with col_f:
            uploaded_file = st.file_uploader(
                "📂 Wgraj e-Sprawozdanie (XML / XAdES / HTML)",
                type=['xml', 'xades', 'html', 'htm']
            )

    # ── WYBÓR WSKAŹNIKÓW ───────────────────────────────────────────────────
    st.markdown("### 🎛️ Wybierz wskaźniki do analizy")

    # Inicjalizacja zaznaczenia w session_state
    if 'wybrane_wskazniki' not in st.session_state:
        # Domyślne: cała płynność + rentowność + zadłużenie
        st.session_state['wybrane_wskazniki'] = (
            GRUPY_WSKAZNIKOW["Płynność"]
            + GRUPY_WSKAZNIKOW["Rentowność"]
            + GRUPY_WSKAZNIKOW["Zadłużenie"]
        )

    # Szybkie presety
    col_p1, col_p2, col_p3, col_p4 = st.columns(4)
    with col_p1:
        if st.button("✅ Zaznacz wszystkie", use_container_width=True):
            st.session_state['wybrane_wskazniki'] = [
                wid for wids in GRUPY_WSKAZNIKOW.values() for wid in wids
            ]
            st.rerun()
    with col_p2:
        if st.button("📦 Podstawowe (15)", use_container_width=True):
            st.session_state['wybrane_wskazniki'] = (
                GRUPY_WSKAZNIKOW["Płynność"][:3]
                + GRUPY_WSKAZNIKOW["Rentowność"][:4]
                + GRUPY_WSKAZNIKOW["Zadłużenie"][:4]
                + GRUPY_WSKAZNIKOW["Sprawność"][:4]
            )
            st.rerun()
    with col_p3:
        if st.button("🏦 Kredyt bankowy", use_container_width=True):
            st.session_state['wybrane_wskazniki'] = (
                GRUPY_WSKAZNIKOW["Płynność"]
                + ["debt_ratio", "dte", "icr", "debt_to_ebitda", "net_debt_ratio"]
                + ["altman_z_score", "springate_score"]
            )
            st.rerun()
    with col_p4:
        if st.button("❌ Wyczyść wybór", use_container_width=True):
            st.session_state['wybrane_wskazniki'] = []
            st.rerun()

    # Checkboxy pogrupowane
    for grupa_nazwa, wskazniki_id in GRUPY_WSKAZNIKOW.items():
        with st.expander(f"**{grupa_nazwa}** ({len(wskazniki_id)} wskaźników)", expanded=True):
            cols = st.columns(2)
            for i, wid in enumerate(wskazniki_id):
                defn = WSKAZNIKI_DEFINICJE[wid]
                checked = wid in st.session_state['wybrane_wskazniki']
                nowa_wart = cols[i % 2].checkbox(
                    f"{defn['nazwa']}",
                    value=checked,
                    key=f"cb_{wid}",
                    help=f"Wzór: {defn['wzor']}"
                )
                if nowa_wart and wid not in st.session_state['wybrane_wskazniki']:
                    st.session_state['wybrane_wskazniki'].append(wid)
                elif not nowa_wart and wid in st.session_state['wybrane_wskazniki']:
                    st.session_state['wybrane_wskazniki'].remove(wid)

    wybrane_id = st.session_state['wybrane_wskazniki']
    st.caption(f"✅ Wybranych wskaźników: **{len(wybrane_id)}** z {sum(len(v) for v in GRUPY_WSKAZNIKOW.values())}")

    # ── ANALIZA PLIKU ──────────────────────────────────────────────────────
    if uploaded_file is None:
        st.info("⬆️ Wgraj plik sprawozdania finansowego, żeby uruchomić analizę.")
        # Pokaż encyklopedię nawet bez pliku
        if wybrane_id:
            st.markdown("---")
            _render_encyclopedia(wybrane_id, branza)
        return

    if not wybrane_id:
        st.warning("⚠️ Zaznacz co najmniej jeden wskaźnik.")
        return

    file_bytes = uploaded_file.read()
    with st.spinner("🔍 Parsowanie struktury sprawozdania…"):
        dane_finansowe, metoda = parse_financial_hybrid(file_bytes, uploaded_file.name)

    if not dane_finansowe:
        st.error("❌ Nie rozpoznano struktury finansowej. Sprawdź, czy plik pochodzi z systemu eKRS.")
        return

    st.success(f"✔️ Sprawozdanie wczytane! Format: **{metoda}**")

    # Dane źródłowe
    with st.expander("🔎 Dane źródłowe (weryfikacja ekstrakcji)"):
        df_raw = pd.DataFrame(
            list(dane_finansowe.items()),
            columns=["Pozycja Bilansowa / RZiS", "Kwota (PLN)"]
        )
        df_raw["Kwota (PLN)"] = df_raw["Kwota (PLN)"].apply(lambda x: f"{x:,.0f}")
        st.dataframe(df_raw, use_container_width=True, hide_index=True)

    # ── OBLICZENIA I WYŚWIETLENIE ──────────────────────────────────────────
    st.markdown(f"### 📊 Wyniki Analizy Wskaźnikowej — {branza}")

    wyniki = compute_all_ratios(dane_finansowe, branza, wybrane_id)
    df_wyniki = pd.DataFrame(wyniki)

    if df_wyniki.empty:
        st.warning("Brak wyników — sprawdź, czy wybrane wskaźniki mają dane w sprawozdaniu.")
        return

    # Tabela z kolorami — kompatybilna z pandas < 2.1 i >= 2.1
    def kolor_statusu(val):
        if "🟢" in str(val): return "background-color: #D5F5E3"
        if "🟡" in str(val): return "background-color: #FEF9E7"
        if "🔴" in str(val): return "background-color: #FADBD8"
        return ""

    def _style(df_in):
        """applymap usunięte w pandas 2.1+ — używamy map() z fallbackiem."""
        styler = df_in.style
        try:
            return styler.map(kolor_statusu, subset=["Status"])
        except AttributeError:
            return styler.applymap(kolor_statusu, subset=["Status"])

    df_display = df_wyniki.drop(columns=["_id"]).copy()
    df_display["Wynik"] = df_display["Wynik"].apply(lambda x: round(float(x), 2))

    # Wyświetlenie per grupa
    for grupa in df_wyniki["Grupa"].unique():
        sub = df_wyniki[df_wyniki["Grupa"] == grupa].drop(columns=["_id"]).copy()
        sub["Wynik"] = sub["Wynik"].apply(lambda x: round(float(x), 2))
        st.markdown(f"#### {grupa}")
        st.dataframe(_style(sub), use_container_width=True, hide_index=True)

    # ── METRYKI PODSUMOWUJĄCE ──────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📋 Podsumowanie audytu")
    ile_zielone = sum(1 for w in wyniki if "🟢" in w["Status"])
    ile_zolte   = sum(1 for w in wyniki if "🟡" in w["Status"])
    ile_czerwone= sum(1 for w in wyniki if "🔴" in w["Status"])
    ile_neutral  = len(wyniki) - ile_zielone - ile_zolte - ile_czerwone

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("🟢 W normie",      ile_zielone)
    mc2.metric("🟡 Wymaga uwagi",  ile_zolte)
    mc3.metric("🔴 Poza normą",    ile_czerwone)
    mc4.metric("⚪ Neutralne",     ile_neutral)

    ocena = (
        "✅ Kondycja dobra"          if ile_czerwone == 0 and ile_zolte <= 2 else
        "⚠️ Kondycja wymaga uwagi"   if ile_czerwone <= 2 else
        "🚨 Kondycja niepokojąca"
    )
    st.markdown(f"**Ogólna ocena CFO: {ocena}**")

    # ── EKSPORT EXCEL ──────────────────────────────────────────────────────
    st.markdown("---")
    excel_bytes = _render_excel(dane_finansowe, df_wyniki, df_raw)
    st.download_button(
        label="📥 Pobierz Audyt CFO (Excel z kolorami)",
        data=excel_bytes,
        file_name=f"Audyt_CFO_{branza.split('/')[0].strip()}_{datetime.now().strftime('%Y%m%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True
    )

    # ── ENCYKLOPEDIA ───────────────────────────────────────────────────────
    st.markdown("---")
    _render_encyclopedia(wybrane_id, branza)


if __name__ == "__main__":
    if st.session_state.get('authenticated', False):
        run_module()
