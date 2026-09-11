import os
import json
import re
import time
import requests
import io
import random
import concurrent.futures
import threading
import PyPDF2
from datetime import datetime

# ---------------------------------------------------------------------------
# ŚCIEŻKI I STAŁE
# ---------------------------------------------------------------------------
FOLDER_DOCELOWY = 'DorAIdca_Radar_Data'
if not os.path.exists(FOLDER_DOCELOWY):
    os.makedirs(FOLDER_DOCELOWY)

PLIK_KONFIGURACJI_M1 = f"{FOLDER_DOCELOWY}/historia_m1.json"
PLIK_REKORDOW_M1     = f"{FOLDER_DOCELOWY}/baza_tresci_m1.json"
PLIK_KONFIGURACJI_M2 = f"{FOLDER_DOCELOWY}/historia_m2.json"
PLIK_REKORDOW_M2     = f"{FOLDER_DOCELOWY}/baza_tresci_m2.json"

SEARCH_API_URL_BASE = (
    "https://eureka.mf.gov.pl/api/public/v1/wyszukiwarka/informacje/"
    "?size={size}&page={page}&sort=parametryPozycjonowania%2Casc"
)
# Rozmiar strony wynikow. Wiekszy = mniej zapytan = mniejsze ryzyko throttlingu
# i szybciej. MF akceptuje 100.
ROZMIAR_STRONY = 100

# PAUZA MIEDZY STRONAMI JEDNEGO OKNA.
#   Bylo 0,2 s, czyli piec zapytan na sekunde. Miesiac VAT-u to ~780 wynikow,
#   czyli osiem stron — szly w poltorej sekundy, a przy uzupelnianiu dwoch lat
#   wstecz takich serii bylo dwadziescia kilka pod rzad. 10 wrzesnia 2026 MF
#   odcielo na tym caly adres biura: nie tylko aplikacje, ale i zwykle wejscie
#   na Eureke z przegladarki, wszystkim w sieci.
#
#   Poltorej sekundy wydluza pobranie miesiaca o kilkanascie sekund. To cena,
#   ktorej nie warto negocjowac — blokada kosztowala pol dnia.
PAUZA_MIEDZY_STRONAMI_S = float(os.environ.get("MF_PAUZA_STRONY_S", "1.5"))
# Twardy limit glebokosci paginacji po stronie MF: API zwykle nie pozwala
# zejsc glebiej niz ~1000 wynikow (page*size). Gdy total_hits przekracza ten
# prog, pojedyncze zapytanie NIE zdola pobrac wszystkiego - trzeba podzielic
# okres na mniejsze przedzialy (patrz pobierz_wszystko_z_okresu -> auto-split).
MAKS_GLEBOKOSC_MF = 1000
PDF_API_URL   = "https://eureka.mf.gov.pl/api/public/v1/informacje/{id}/eksport/pdf"
# ► NOWE: endpoint HTML (lżejszy niż PDF, bez renderowania)
HTML_API_URL  = "https://eureka.mf.gov.pl/informacje/podglad/{id}"
PODGLAD_URL   = "https://eureka.mf.gov.pl/informacje/podglad/{id}"

FRAZY_KLUCZOWE = [
    "sieć ciepłownicza", "przebudowa sieci", "przyłącze", "węzeł cieplny",
    "taryfa dla ciepła", "wodociąg", "kanalizacja", "oczyszczalnia ścieków",
    "stacja uzdatniania", "spółka komunalna"
]

# UWAGA: ten slownik byl wczesniej (blednie) uzywany jako filtr API przez
# sprawdzanie podciagu w polu SYG. NIE jest to prawdziwy mechanizm filtrowania
# uzywany przez API MF - prawdziwy filtr to KODY_PRZEPISOW ponizej. Ten slownik
# zostaje tylko jako pomocniczy (np. do wyswietlania prefiksu w UI), ale nigdy
# nie powinien byc przekazywany do pobierz_wszystko_z_okresu / szukaj_w_api_mf.
KODY_PODATKOW = {"PIT": ".4011.", "CIT": ".4010.", "VAT": ".4012.", "AKCYZA": ".4013.",
                 "PCC": ".4014."}

# ► KLUCZOWE: prawdziwy filtr uzywany przez strone eureka.mf.gov.pl do
# zawezania wynikow do konkretnego podatku. To NIE jest filtrowanie po
# sygnaturze (jak bylo wczesniej) tylko po wewnetrznym ID aktu prawnego
# w slowniku MF. Wartosci znalezione przez podsluchanie zapytan sieciowych
# prawdziwej wyszukiwarki (DevTools -> Network -> Payload -> filter.PRZEPISY).
KODY_PRZEPISOW = {
    "PIT":    29903,
    "VAT":    29955,
    "CIT":    29985,
    "AKCYZA": 38830,
    "PCC":    31693,
}


# ---------------------------------------------------------------------------
# DATY STARTOWE PODATKOW
# ---------------------------------------------------------------------------
# Kazdy podatek ma prog daty wydania, ponizej ktorego dokumentow NIE zbieramy.
# Nie jest to jedna wspolna data, bo podatki dochodza do systemu w roznych
# momentach — PCC wchodzi 3 sierpnia 2026 i swiadomie NIE ciagniemy historii.
#
# DLACZEGO OSOBNE DATY, A NIE JEDNA WSPOLNA
#   Przy jednej wspolnej dacie (15.07) moduly liczylyby braki PCC juz od
#   polowy lipca i pokazywaly "Bez streszczenia: 0" tam, gdzie prawda brzmi
#   "jeszcze nie zbieramy". Rozroznienie "brak danych" od "zero brakow" jest
#   tu istotne — inaczej nie widac, czy automat dziala, czy stoi.
#
# To JEDYNE miejsce, w ktorym te daty sa zapisane. Moduly wolaja data_start().
DATA_START_DOMYSLNA = "2026-07-15"

DATY_START_PODATKU = {
    "PCC": "2026-08-03",
}


def data_start(podatek: str | None = None) -> str:
    """Prog daty wydania dla podatku (YYYY-MM-DD).

    Bez argumentu zwraca date domyslna. Dla podatku bez wlasnego wpisu tez
    zwraca domyslna — dodanie kolejnego podatku bez daty startowej po prostu
    zadziala jak dotad, zamiast wysypac sie na KeyError."""
    if not podatek:
        return DATA_START_DOMYSLNA
    return DATY_START_PODATKU.get(podatek.upper(), DATA_START_DOMYSLNA)

MIESIACE_PL   = [
    "Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
    "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień"
]

# ---------------------------------------------------------------------------
# USTAWIENIA SZYBKOŚCI
# ---------------------------------------------------------------------------
# Liczba równoległych wątków pobierających PDF/HTML.
# Tryb NIEZAWODNY: mniej watkow + wieksze opoznienia = serwer MF rzadziej
# blokuje IP przy masowym pobieraniu tresci (kluczowe na stalym IP GitHub Actions).
WORKERS_POBIERANIA = 3          # bylo 5 - mniej rownoleglosci = lagodniej dla MF
OPOZNIENIE_MIN     = 0.8
OPOZNIENIE_MAX     = 1.6
TIMEOUT_PDF        = 15         # sekund
TIMEOUT_HTML       = 10

# ── Reakcja na blokade IP w trakcie pobierania tresci ──────────────────────
# Gdy MF zablokuje IP (429/403), NIE porzucamy calej partii. Watki robia
# pauze i wznawiaja prace (backoff), a dokumenty ktore trafily na blokade
# wracaja na liste "do ponowienia" - dzieki temu jedna blokada nie kasuje
# setek dokumentow (dawniej: pierwsza blokada ubijala cala partie).
PAUZA_PO_BLOKADZIE_S      = 45   # ile watek czeka po wykryciu blokady, zanim wznowi
MAKS_PAUZ_BLOKADY         = 4    # ile razy w obrebie jednej partii pozwalamy na pauze

# Blokada do bezpiecznego dostępu do listy wyników z wątków
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# FUNKCJE BAZODANOWE
# ---------------------------------------------------------------------------
def wczytaj_historie(plik):
    if os.path.exists(plik):
        with open(plik, 'r', encoding='utf-8') as f:
            dane = json.load(f)
            dane.setdefault("uszkodzone_id", [])
            dane.setdefault("ukonczone_kombinacje", [])
            return dane
    return {"przetworzone_id": [], "ukonczone_kombinacje": [], "uszkodzone_id": []}

def zapisz_historie(plik, konfiguracja):
    with open(plik, 'w', encoding='utf-8') as f:
        json.dump(konfiguracja, f, ensure_ascii=False, indent=4)

def wczytaj_pelne_tresci(plik):
    if os.path.exists(plik):
        with open(plik, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def zapisz_pelne_tresci(plik, lista_rekordow):
    with open(plik, 'w', encoding='utf-8') as f:
        json.dump(lista_rekordow, f, ensure_ascii=False, indent=4)

def wyczysc_dane_serwera(plik_konf, plik_rekordow):
    if os.path.exists(plik_konf):    os.remove(plik_konf)
    if os.path.exists(plik_rekordow): os.remove(plik_rekordow)

def wyczysc_tekst_dla_worda(tekst):
    if not tekst: return ""
    # Usuwa znaki niedozwolone w XML/docx
    return re.sub(
        r'[^\x09\x0A\x0D\x20-\x7E\x85\xA0-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]',
        '', tekst
    )

# ---------------------------------------------------------------------------
# ► NOWE: OSZCZĘDNOŚĆ MIEJSCA — normalizacja tekstu przed zapisem do bazy
# ---------------------------------------------------------------------------
def _wyczysc_i_znormalizuj_tekst(tekst: str) -> str:
    """
    Redukuje rozmiar tekstu PRZED zapisaniem go do bazy, bez utraty tresci
    merytorycznej:

    1. Usuwa linie powtarzajace sie 3+ razy w obrebie jednego dokumentu.
       Wielostronicowe PDF-y maja nagłowek/stopke/sygnature powtorzona
       RAZ NA KAZDA STRONE - to czysty artefakt ekstrakcji. Tresc prawna
       praktycznie nigdy nie powtarza sie doslownie 3+ razy w jednym
       dokumencie, wiec ten prog jest bezpieczny.
    2. Redukuje wielokrotne puste linie do pojedynczej.
    3. Usuwa biale znaki na koncach linii.

    Bezpieczne dla wyszukiwania pelnotekstowego i integracji z GPT - to
    wciaz zwykly tekst plain-text, tylko bez smieci ekstrakcji.
    """
    if not tekst:
        return tekst

    linie = tekst.split('\n')

    # Policz wystapienia kazdej niepustej linii (min. 3 znaki - pomija
    # pojedyncze cyfry/znaki interpunkcyjne ktore moga powtarzac sie
    # naturalnie, np. numeracja podpunktow "a)", "b)")
    licznik: dict[str, int] = {}
    for linia in linie:
        znormalizowana = linia.strip()
        if len(znormalizowana) >= 3:
            licznik[znormalizowana] = licznik.get(znormalizowana, 0) + 1

    # Linie powtarzajace sie 3+ razy = prawdopodobnie naglowek/stopka/watermark
    powtarzajace_sie = {l for l, n in licznik.items() if n >= 3}

    wynik = []
    juz_uzyte = set()
    for linia in linie:
        znormalizowana = linia.strip()
        if znormalizowana in powtarzajace_sie:
            if znormalizowana not in juz_uzyte:
                wynik.append(linia)
                juz_uzyte.add(znormalizowana)
            # kolejne wystapienia tej samej linii pomijamy
        else:
            wynik.append(linia)

    tekst_oczyszczony = '\n'.join(wynik)

    # Redukcja 3+ pustych linii pod rzad do pojedynczej pustej linii
    tekst_oczyszczony = re.sub(r'\n{3,}', '\n\n', tekst_oczyszczony)
    # Usuniecie bialych znakow na koncach kazdej linii
    tekst_oczyszczony = '\n'.join(l.rstrip() for l in tekst_oczyszczony.split('\n'))

    return tekst_oczyszczony.strip()


# ---------------------------------------------------------------------------
# ► NOWE: EKSTRAKCJA TEKSTU Z HTML (szybsza niż PDF)
# ---------------------------------------------------------------------------
def _pobierz_tekst_html(id_dokumentu, sesja=None):
    """
    Pobiera stronę HTML podglądu i wyciąga czysty tekst.
    Zwraca (tekst, status).  Status: "OK" | "BRAK_PLIKU" | "BLOKADA"
    """
    from bs4 import BeautifulSoup
    url     = HTML_API_URL.format(id=id_dokumentu)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
    caller  = sesja if sesja else requests
    try:
        r = caller.get(url, headers=headers, timeout=TIMEOUT_HTML)
        if r.status_code == 200:
            soup  = BeautifulSoup(r.text, 'lxml')
            # Usuwa nagłówki nawigacyjne, zostawia treść dokumentu
            for tag in soup.select("nav, header, footer, script, style, .breadcrumb"):
                tag.decompose()
            tekst = soup.get_text(separator="\n", strip=True)
            # Jeśli tekst jest bardzo krótki — strona błędu
            if len(tekst) < 200:
                return None, "BRAK_PLIKU"
            return tekst, "OK"
        elif r.status_code in (404, 400):
            return None, "BRAK_PLIKU"
        elif r.status_code == 429:
            return None, "BLOKADA"
        else:
            return None, "BLOKADA"
    except Exception:
        return None, "BLOKADA"

# ---------------------------------------------------------------------------
# EKSTRAKCJA TEKSTU Z PDF (ulepszona — fallback po HTML)
# ---------------------------------------------------------------------------
def pobierz_tekst_pdf(id_dokumentu, sesja=None):
    """
    Próbuje pobrać dokument w kolejności:
      1. HTML (szybciej, mniej obciążeń serwera, z natury "chudszy" - brak
         podziału na strony, więc brak powtórzeń nagłówków/stopek)
      2. PDF (pełna wierność, jako fallback)
    Tekst jest normalizowany (patrz _wyczysc_i_znormalizuj_tekst) przed
    zwróceniem - usuwa artefakty ekstrakcji, realnie zmniejszając rozmiar
    zapisywany w bazie danych.
    Zwraca (tekst, status).
    """
    # --- próba HTML ---
    tekst_html, status_html = _pobierz_tekst_html(id_dokumentu, sesja)
    if tekst_html:
        return _wyczysc_i_znormalizuj_tekst(tekst_html), "OK"
    if status_html == "BLOKADA":
        return None, "BLOKADA"     # nie próbujemy PDF jeśli IP zablokowane

    # --- fallback: PDF ---
    url     = PDF_API_URL.format(id=id_dokumentu)
    headers = {"User-Agent": "Mozilla/5.0"}
    caller  = sesja if sesja else requests
    for proba in range(3):
        try:
            r = caller.get(url, headers=headers, timeout=TIMEOUT_PDF)
            if r.status_code == 200:
                plik   = io.BytesIO(r.content)
                reader = PyPDF2.PdfReader(plik)
                tekst  = "\n".join(
                    p.extract_text() or "" for p in reader.pages
                ).strip()
                if tekst:
                    return _wyczysc_i_znormalizuj_tekst(tekst), "OK"
                return None, "BRAK_PLIKU"   # PDF bez tekstu (skany)
            elif r.status_code in (404, 400):
                return None, "BRAK_PLIKU"
            elif r.status_code == 429:
                time.sleep(5 + proba * 3)
            else:
                time.sleep(2)
        except Exception:
            time.sleep(3)
    return None, "BLOKADA"

# ---------------------------------------------------------------------------
# ► RÓWNOLEGŁE POBIERANIE DOKUMENTÓW (tryb niezawodny z reakcja na blokade)
# ---------------------------------------------------------------------------
def pobierz_dokumenty_rownolegle(
    lista_dokumentow: list,
    przetworzone_id: set,
    uszkodzone_id: set,
    callback_postep=None,   # fn(idx, total, sygnatura, status)
    workers: int = WORKERS_POBIERANIA,
    log_fn=None,
) -> tuple:
    """
    Pobiera dokumenty równolegle w `workers` wątkach, w trybie NIEZAWODNYM.

    Reakcja na blokade IP (429/403 z MF): zamiast porzucic cala partie po
    pierwszej blokadzie (dawne zachowanie: "555 do pobrania -> 56 zapisanych"),
    watki robia PAUZE i WZNAWIAJA prace. Dokument, ktory trafil na blokade,
    jest ponawiany; jesli po wznowieniu nadal sie nie udaje, trafia na liste
    `do_ponowienia` (a NIE `uszkodzone`) - warstwa wyzej moze go pozniej
    dociagnac w kolejnej probie calego zadania.

    Rozroznienie statusow ma znaczenie:
      - `uszkodzone`    = dokument NIE ISTNIEJE w pobieralnej formie (skan bez
                          tekstu, brak HTML i PDF) -> nie ma sensu ponawiac
      - `do_ponowienia` = dokument istnieje, ale zablokowano IP / timeout
                          -> warto ponowic pozniej

    Zwraca:
        (pelne_tresci, nowe_przetworzone_id, nowe_uszkodzone_id,
         byla_blokada: bool, do_ponowienia_id: list)
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    pelne_tresci       = []
    nowe_przetworzone  = []
    nowe_uszkodzone    = []
    do_ponowienia      = []
    total              = len(lista_dokumentow)

    # Licznik pauz po blokadzie - wspoldzielony miedzy watkami.
    pauzy_uzyte        = {"n": 0}
    byla_blokada       = {"tak": False}
    _pauza_lock        = threading.Lock()

    def _obsluz_blokade() -> bool:
        """
        Wolane przez watek, ktory dostal BLOKADE. Robi pauze (jesli nie
        wyczerpano limitu) i zwraca True, jesli watek powinien PONOWIC
        dokument, albo False, jesli limit pauz wyczerpany (poddajemy dokument).
        """
        with _pauza_lock:
            byla_blokada["tak"] = True
            if pauzy_uzyte["n"] >= MAKS_PAUZ_BLOKADY:
                return False
            pauzy_uzyte["n"] += 1
            numer = pauzy_uzyte["n"]
        _log(f"    ⏸ Wykryto blokade IP MF — pauza {PAUZA_PO_BLOKADZIE_S}s "
             f"({numer}/{MAKS_PAUZ_BLOKADY}), potem wznawiam...")
        time.sleep(PAUZA_PO_BLOKADZIE_S)
        return True

    def _pobierz_jeden(args):
        idx, dok = args
        # Rozlozenie startow w czasie + losowy jitter (lagodniej dla serwera)
        time.sleep((idx % workers) * 0.2 + random.uniform(OPOZNIENIE_MIN, OPOZNIENIE_MAX))
        with requests.Session() as s:
            tekst, status = pobierz_tekst_pdf(dok["id"], sesja=s)
            # Jesli blokada - sprobuj pauzy i JEDNEGO ponowienia w tym samym watku
            if status == "BLOKADA":
                if _obsluz_blokade():
                    tekst, status = pobierz_tekst_pdf(dok["id"], sesja=s)
        return idx, dok, tekst, status

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_pobierz_jeden, (i, d)): (i, d)
            for i, d in enumerate(lista_dokumentow)
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            idx, dok, tekst, status = future.result()
            completed += 1

            with _lock:
                if tekst:
                    rekord = {
                        "Data":      dok["data"],
                        "Podatek":   dok["typ"],
                        "Sygnatura": dok["sygnatura"],
                        "Link":      PODGLAD_URL.format(id=dok["id"]),
                        "Tekst":     tekst,
                        "Format":    "HTML+PDF",
                        "Pobrano":   datetime.now().isoformat(timespec='seconds')
                    }
                    pelne_tresci.append(rekord)
                    nowe_przetworzone.append(dok["id"])
                elif status == "BLOKADA":
                    # Nadal blokada po pauzie/ponowieniu -> do ponowienia pozniej
                    do_ponowienia.append(dok["id"])
                elif status in ("BRAK_PLIKU", "BŁĄD_CZYTANIA"):
                    # Dokument nie istnieje w pobieralnej formie -> trwaly brak
                    nowe_uszkodzone.append(dok["id"])
                else:
                    do_ponowienia.append(dok["id"])

            if callback_postep:
                callback_postep(completed, total, dok["sygnatura"], status)

    # Sortuj wyniki wg oryginalnej kolejności (as_completed miesza kolejność)
    id_order = {d["id"]: i for i, d in enumerate(lista_dokumentow)}
    pelne_tresci.sort(key=lambda r: id_order.get(
        next((d["id"] for d in lista_dokumentow if PODGLAD_URL.format(id=d["id"]) == r["Link"]), ""),
        0
    ))

    if do_ponowienia:
        _log(f"    ℹ {len(do_ponowienia)} dokumentow do ponowienia (blokada/timeout), "
             f"{len(nowe_uszkodzone)} trwale niedostepnych (brak pliku).")

    return (pelne_tresci, nowe_przetworzone, nowe_uszkodzone,
            byla_blokada["tak"], do_ponowienia)

# ---------------------------------------------------------------------------
# POBIERANIE LISTY DOKUMENTÓW Z API MF (bez zmian w logice, tylko cleanup)
# ---------------------------------------------------------------------------
# Rotacja User-Agent - mniejsze ryzyko blokady
_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]
_ua_idx = 0

def _wykonaj_zapytanie_api(sesja, url, payload, timeout=15):
    """
    Wspólna logika zapytania POST do Eureka API z retry i rotacja UA.
    Zwraca (wyniki: list, status: str, total_hits: int|None).
    total_hits pochodzi z pola "totalHits" w odpowiedzi API - pozwala
    precyzyjnie wiedziec ile lacznie dokumentow jest dla danego zapytania,
    zamiast zgadywac po dlugosci strony.
    """
    global _ua_idx
    MAKS_PROB = 3

    for proba in range(MAKS_PROB):
        try:
            ua = _UA_LIST[_ua_idx % len(_UA_LIST)]
            _ua_idx += 1
            r = sesja.post(
                url, json=payload,
                headers={
                    "User-Agent": ua,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8",
                    "Origin": "https://eureka.mf.gov.pl",
                    "Referer": "https://eureka.mf.gov.pl/",
                },
                timeout=timeout
            )

            if r.status_code == 200:
                dane   = r.json()
                wyniki = dane.get("content") or dane.get("items") or []
                if not wyniki:
                    for v in dane.values():
                        if (isinstance(v, list) and v
                                and isinstance(v[0], dict)
                                and ("id" in v[0] or "ID_INFORMACJI" in v[0])):
                            wyniki = v
                            break

                total_hits = (
                    dane.get("totalHits")
                    or dane.get("totalElements")
                    or dane.get("total")
                )

                return wyniki, "OK", total_hits

            elif r.status_code == 429:
                # Rate limit - czekaj coraz dluzej
                czas = 10 * (proba + 1)
                time.sleep(czas)
                continue

            elif r.status_code in (403, 503):
                # Potencjalna blokada - dluzsze czekanie
                time.sleep(15 * (proba + 1))
                continue

            else:
                return [], f"ERROR_{r.status_code}", None

        except requests.exceptions.Timeout:
            time.sleep(5)
            continue
        except Exception:
            time.sleep(3)
            continue

    return [], "ERROR", None

def _mapuj_dokument(d, nazwa_podatku):
    """
    Mapuje surowy rekord z API na nasz format.
    UWAGA: filtrowanie po podatku NIE odbywa sie juz tutaj (po sygnaturze) -
    odbywa sie po stronie API przez filtr PRZEPISY w samym zapytaniu.
    Ta funkcja tylko przeksztalca format, nie odrzuca dokumentow.
    """
    sygnatura = str(d.get('SYG', '')).upper()
    doc_id = str(d.get('id') or d.get('ID_INFORMACJI') or '')
    if not doc_id:
        return None
    return {
        "id":        doc_id,
        "sygnatura": sygnatura,
        "typ":       nazwa_podatku,
        "data":      str(d.get('DT_WYD', '')).split('T')[0]
    }

def _pobierz_jedno_okno(data_start_str, data_koniec_str, sesja, nazwa_podatku,
                        kod_przepisu, log_fn=None):
    """
    Pobiera WSZYSTKIE strony dla JEDNEGO przedzialu dat, bez dzielenia.
    Zwraca (dokumenty, status, total_hits).

    Statusy:
      "OK"                – pobrano tyle, ile API zadeklarowalo w totalHits
      "NIEPELNE_POBRANIE" – API zwrocilo mniej niz totalHits (pusta strona
                            w srodku / throttling / cichy blad)
      "LIMIT_GLEBOKOSCI"  – totalHits > MAKS_GLEBOKOSC_MF: pojedyncze okno
                            nie jest w stanie pobrac wszystkiego, wywolujacy
                            musi podzielic okres
      "ERROR"             – twardy blad API
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    dokumenty  = []
    page       = 0
    total_hits = None
    puste_z_rzedu = 0
    MAKS_PUSTYCH_Z_RZEDU = 2   # pusta strona ponawiana zanim uznamy ja za koniec

    while True:
        url     = SEARCH_API_URL_BASE.format(size=ROZMIAR_STRONY, page=page)
        payload = {
            "query": "",
            "filter": {
                "KATEGORIA_INFORMACJI": [1],
                "PRZEPISY":     [kod_przepisu],
                "DT_WYD_start": data_start_str,
                "DT_WYD_end":   data_koniec_str
            },
            "columns": ["SYG", "ID_INFORMACJI", "DT_WYD", "KATEGORIA_INFORMACJI"],
            "searchInFullPhrase": True,
            "searchInContent":    False,
            "searchInSynonyms":   False,
            "warunkiDodatkowe":   []
        }
        wyniki, status, total_hits_strona = _wykonaj_zapytanie_api(sesja, url, payload, timeout=15)
        if status == "ERROR":
            _log(f"    [strona {page}] BLAD API — przerywam okno.")
            return dokumenty, "ERROR", total_hits

        if total_hits is None and total_hits_strona is not None:
            total_hits = total_hits_strona
            _log(f"    API zglasza total_hits={total_hits} dla okna {data_start_str}..{data_koniec_str}")
            # Jesli MF deklaruje wiecej niz zdolamy przewinac - sygnalizuj od razu
            if total_hits > MAKS_GLEBOKOSC_MF:
                _log(f"    total_hits={total_hits} > limit glebokosci {MAKS_GLEBOKOSC_MF} — okno wymaga podzialu.")
                return dokumenty, "LIMIT_GLEBOKOSCI", total_hits

        przed = len(dokumenty)
        for d in wyniki:
            dok = _mapuj_dokument(d, nazwa_podatku)
            if dok:
                dokumenty.append(dok)
        dodano = len(dokumenty) - przed
        _log(f"    [strona {page}] pobrano {len(wyniki)} rekordow "
             f"(lacznie {len(dokumenty)}"
             + (f"/{total_hits}" if total_hits is not None else "") + ")")

        # ── Warunek konca: osiagnelismy zadeklarowany total ──
        if total_hits is not None and len(dokumenty) >= total_hits:
            break

        # ── Pusta strona: NIE ufamy jej od razu, ponawiamy ──
        if not wyniki:
            puste_z_rzedu += 1
            if total_hits is not None and len(dokumenty) < total_hits and puste_z_rzedu <= MAKS_PUSTYCH_Z_RZEDU:
                # API zglosilo wiecej niz mamy, a oddalo pusta strone -
                # prawdopodobnie throttling. Czekamy dluzej i ponawiamy TE SAMA strone.
                _log(f"    [strona {page}] PUSTA mimo brakujacych {total_hits - len(dokumenty)} "
                     f"dok. — ponawiam ({puste_z_rzedu}/{MAKS_PUSTYCH_Z_RZEDU}) po pauzie...")
                time.sleep(6 * puste_z_rzedu)
                continue
            # Albo nie znamy total (fallback), albo wyczerpalismy ponowienia:
            break
        else:
            puste_z_rzedu = 0

        # Fallback gdy API nie zwraca totalHits: krotka strona = koniec
        if total_hits is None and len(wyniki) < ROZMIAR_STRONY:
            break

        page += 1
        time.sleep(PAUZA_MIEDZY_STRONAMI_S)

    if total_hits is not None and len(dokumenty) < total_hits:
        return dokumenty, "NIEPELNE_POBRANIE", total_hits
    return dokumenty, "OK", total_hits


def _podziel_okres(data_start_str, data_koniec_str):
    """
    Dzieli przedzial [start, koniec] na dwie mniej wiecej rowne polowy (po dacie).
    Zwraca liste [(start1, koniec1), (start2, koniec2)] lub [] gdy przedzial
    jest juz 1-dniowy (nie da sie podzielic).
    """
    d0 = datetime.strptime(data_start_str, "%Y-%m-%d")
    d1 = datetime.strptime(data_koniec_str, "%Y-%m-%d")
    if d0 >= d1:
        return []
    from datetime import timedelta
    srodek = d0 + (d1 - d0) / 2
    srodek = datetime(srodek.year, srodek.month, srodek.day)
    if srodek <= d0:
        srodek = d0
    # koniec pierwszej polowy = srodek; poczatek drugiej = srodek + 1 dzien
    kon1 = srodek.strftime("%Y-%m-%d")
    st2  = (srodek + timedelta(days=1)).strftime("%Y-%m-%d")
    if st2 > data_koniec_str:
        return []
    return [(data_start_str, kon1), (st2, data_koniec_str)]


def pobierz_wszystko_z_okresu(data_start_str, data_koniec_str, sesja, nazwa_podatku,
                              kod_przepisu, log_fn=None, _poziom=0):
    """
    kod_przepisu: numeryczny ID aktu prawnego z KODY_PRZEPISOW (np. 29903 dla PIT).
    To jest PRAWDZIWY filtr uzywany przez API - identyczny z tym, czego
    uzywa strona eureka.mf.gov.pl przy wyszukiwaniu.

    Pobiera cala liste dokumentow dla okresu. Jesli MF zglasza wiecej wynikow
    niz da sie przewinac (limit glebokosci paginacji), AUTOMATYCZNIE dzieli
    okres na mniejsze przedzialy i scala wyniki (z deduplikacja po id).
    Dzieki temu duze okna (np. caly miesiac VAT) nie sa juz ucinane.

    log_fn: opcjonalna funkcja logujaca (np. print albo callback Streamlit).
    Zwraca (dokumenty, status: "OK"|"NIEPELNE_POBRANIE"|"ERROR").
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    wciecie = "  " * _poziom
    _log(f"{wciecie}[{nazwa_podatku}] Okno {data_start_str}..{data_koniec_str}")

    dokumenty, status, total_hits = _pobierz_jedno_okno(
        data_start_str, data_koniec_str, sesja, nazwa_podatku, kod_przepisu, log_fn=_log
    )

    # ── Okno miesci sie w limicie i pobralo komplet ──
    if status == "OK":
        return dokumenty, "OK"

    # ── Limit glebokosci LUB niepelne pobranie: sprobuj podzielic okres ──
    if status in ("LIMIT_GLEBOKOSCI", "NIEPELNE_POBRANIE"):
        podokresy = _podziel_okres(data_start_str, data_koniec_str)
        if podokresy and _poziom < 6:
            _log(f"{wciecie}[{nazwa_podatku}] Status {status} — dziele okno na "
                 f"{len(podokresy)} czesci i pobieram osobno.")
            scalone = {d["id"]: d for d in dokumenty}   # zachowaj to, co juz mamy
            czy_blad = False
            for (s, k) in podokresy:
                pod_dok, pod_status = pobierz_wszystko_z_okresu(
                    s, k, sesja, nazwa_podatku, kod_przepisu,
                    log_fn=log_fn, _poziom=_poziom + 1,
                )
                for d in pod_dok:
                    scalone[d["id"]] = d
                if pod_status not in ("OK",):
                    czy_blad = True
            wynik = list(scalone.values())
            _log(f"{wciecie}[{nazwa_podatku}] Po podziale: {len(wynik)} unikalnych dokumentow.")
            return wynik, ("NIEPELNE_POBRANIE" if czy_blad else "OK")
        else:
            # Nie da sie juz podzielic (okno 1-dniowe) albo za gleboka rekurencja
            _log(f"{wciecie}[{nazwa_podatku}] Status {status}, nie moge dalej dzielic "
                 f"(okno={data_start_str}..{data_koniec_str}). Zwracam {len(dokumenty)} dok.")
            return dokumenty, "NIEPELNE_POBRANIE"

    # ── Twardy blad ──
    return dokumenty, "ERROR"

def szukaj_w_api_mf(data_start_str, data_koniec_str, fraza, sesja, nazwa_podatku, kod_przepisu):
    """
    kod_przepisu: numeryczny ID aktu prawnego z KODY_PRZEPISOW (np. 29903 dla PIT).
    """
    dokumenty  = []
    page       = 0
    total_hits = None

    while True:
        url     = SEARCH_API_URL_BASE.format(page=page)
        payload = {
            "query": fraza,
            "filter": {
                "KATEGORIA_INFORMACJI": [1],
                "PRZEPISY":     [kod_przepisu],
                "DT_WYD_start": data_start_str,
                "DT_WYD_end":   data_koniec_str
            },
            "columns": ["SYG", "ID_INFORMACJI", "DT_WYD", "KATEGORIA_INFORMACJI"],
            "searchInFullPhrase": False,
            "searchInContent":    True,
            "searchInSynonyms":   True,
            "warunkiDodatkowe":   []
        }
        wyniki, status, total_hits_strona = _wykonaj_zapytanie_api(sesja, url, payload, timeout=12)
        if status == "ERROR":
            return dokumenty, "ERROR"

        if total_hits is None and total_hits_strona is not None:
            total_hits = total_hits_strona

        for d in wyniki:
            dok = _mapuj_dokument(d, nazwa_podatku)
            if dok:
                dokumenty.append(dok)

        if total_hits is not None:
            if len(dokumenty) >= total_hits or not wyniki:
                break
        else:
            if len(wyniki) < 25:
                break

        page += 1
        time.sleep(PAUZA_MIEDZY_STRONAMI_S)

    return dokumenty, "OK"
