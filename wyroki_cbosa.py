"""
wyroki_cbosa.py — Scraper Centralnej Bazy Orzeczen Sadow Administracyjnych
(orzeczenia.nsa.gov.pl). Rdzen modulu Wyroki (modul 4 DorAIdca Radar).

ARCHITEKTURA (dlaczego tak):
  CBOSA nie ma API — to stara aplikacja formularzowa (HTML + sesja).
  Zapytanie = POST formularza, wyniki = stronicowane HTML (/cbo/find?p=N),
  szczegoly = /doc/<ID>. Dlatego:

  1. DYNAMICZNE ODKRYWANIE POL FORMULARZA: nie zgadujemy nazw inputow.
     Przy starcie pobieramy strone formularza i mapujemy pola po kluczach
     pomocy p3Help('klucz') stojacych przy kazdym polu (klucze sa stabilne:
     'symbole', 'data_orzeczenia', 'z_uzasadnieniem', 's_prawomocne'...).
     Dzieki temu zmiana nazw pol przez NSA nie psuje scrapera, o ile
     klucze pomocy zostana.

  2. SESJA: paginacja /cbo/find?p=N dziala w kontekscie sesji (cookie),
     w ktorej zyje ostatnie zapytanie — wszystko robimy na jednym
     requests.Session.

  3. TEMPO: celowo sekwencyjnie i powoli (PAUZA_S między zadaniami).
     To stary system publiczny — traktujemy go z szacunkiem. Zadnych
     watkow, zadnego rownoleglego pobierania.

SYMBOLE SPRAW (klasyfikacja sadowa) — filtr glowny:
  6110 = Podatek od towarow i uslug (VAT)   [potwierdzone na zywych danych]
  6112 = Podatek dochodowy od osob fizycznych (PIT)
  6113 = Podatek dochodowy od osob prawnych (CIT)
  6111 = Podatek akcyzowy (AKCYZA)
  6116 = Podatek od czynnosci cywilnoprawnych, oplata skarbowa oraz inne
         podatki i oplaty (PCC) — symbol SZERSZY niz sam PCC, patrz
         komentarz przy SYMBOLE_PODATKOW
  Tryb kalibracji wypisuje opisy symboli z pobranych dokumentow — pierwsze
  uruchomienie zweryfikuje te mape na zywych danych.
"""

import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BAZA_URL   = "https://orzeczenia.nsa.gov.pl"
QUERY_URL  = f"{BAZA_URL}/cbo/query"
FIND_URL   = f"{BAZA_URL}/cbo/find"
DOC_URL    = f"{BAZA_URL}/doc/{{id}}"

USER_AGENT = ("Mozilla/5.0 (compatible; DorAIdca-Radar-Archiwum/1.0; "
              "prywatne archiwum doradcy podatkowego)")

# Naglowki "przegladarkowo-kompatybilne": czesc starszych systemow rzadowych
# odrzuca polaczenia bez naglowka zaczynajacego sie od Mozilla/5.0 zanim
# w ogole odpowie (stad RemoteDisconnected). Konwencja "Mozilla/5.0
# (compatible; NazwaBota; ...)" to standardowy, przejrzysty sposob
# identyfikacji uzywany przez legalne roboty (Googlebot, Bingbot).
NAGLOWKI_SESJI = {
    "User-Agent": USER_AGENT,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
    "Connection": "keep-alive",
}

PAUZA_S            = 1.5   # miedzy zadaniami HTTP — celowo wolno
TIMEOUT_S          = 30
MAKS_PROB_HTTP     = 3
PAUZA_PO_BLEDZIE_S = 20
MAKS_STRON         = 400   # bezpiecznik na wypadek petli paginacji

SYMBOLE_PODATKOW = {
    "VAT":    "6110",
    "AKCYZA": "6111",
    "PIT":    "6112",
    "CIT":    "6113",
    # 6116 = "Podatek od czynnosci cywilnoprawnych, oplata skarbowa oraz inne
    # podatki i oplaty". UWAGA: symbol jest SZERSZY niz sam PCC — zlapie tez
    # sprawy o oplate skarbowa i "inne podatki i oplaty" (np. oplata
    # targowa, uzdrowiskowa). Swiadomie nie zawezamy dalej: CBOSA nie ma
    # symbolu wylacznie dla PCC, a odsiewanie po tresci sentencji dawaloby
    # falszywe negatywy. Nadmiarowe wyroki lepiej odfiltrowac wzrokiem
    # w module Wyroki niz stracic trafienie.
    "PCC":    "6116",
}


class BladCBOSA(Exception):
    pass


# ---------------------------------------------------------------------------
# MOST — obejscie blokady adresow GitHub Actions
# ---------------------------------------------------------------------------
# Od 11.09.2026 orzeczenia.nsa.gov.pl odcina adresy runnerow GitHuba: TCP sie
# nawiazuje, po czym serwer zamyka polaczenie bez slowa. Tak samo na porcie 443
# i 80, tak samo dla Pythona, curla i openssl s_client, tak samo na obrazie
# ubuntu-24 i ubuntu-22 — czyli po naszej stronie nie ma czego naprawiac.
# Z Dockera, z bazy Supabase i z funkcji brzegowej Supabase ten sam adres
# odpowiada 200, a sasiedni www.nsa.gov.pl odpowiada nawet z runnera.
#
# Gdy CBOSA_MOST_URL jest ustawiony, zadania ida przez funkcje brzegowa
# (supabase/functions/most-cbosa w repozytorium infra). Bez tej zmiennej — a
# wiec w Dockerze i lokalnie — nic sie nie zmienia i ruch idzie wprost.
MOST_URL   = (os.environ.get("CBOSA_MOST_URL") or "").strip()
MOST_KLUCZ = (os.environ.get("CBOSA_MOST_KLUCZ") or "").strip()
MAKS_PRZEKIEROWAN = 5

# REGION MOSTU — to jest sedno calego obejscia.
#
# Supabase uruchamia funkcje brzegowa w regionie najblizszym WOLAJACEMU, a nie
# w regionie projektu. Zadanie z mojej maszyny trafia wiec do eu-central-1
# i CBOSA odpowiada 200 w 400 ms, a to samo zadanie z runnera GitHuba trafia do
# us-east-1 albo us-east-2 i CBOSA zamyka polaczenie. Zmierzone wprost, jednym
# przebiegiem sondy: bez naglowka region=us-east-1 i blad, z naglowkiem
# region=eu-central-1 i 200 na tej samej maszynie, w tej samej minucie.
#
# Przez trzy nieudane przebiegi wygladalo to na blokade adresow centrow danych
# albo na kare za zbyt gesty ruch. W rzeczywistosci CBOSA odcina ruch spoza
# Europy — takze wtedy, gdy formalnie idzie przez europejski projekt.
MOST_REGION = (os.environ.get("CBOSA_MOST_REGION") or "eu-central-1").strip()


class OdpowiedzMostu:
    """Tyle z interfejsu requests.Response, ile uzywa reszta modulu."""

    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url


def _ciasteczka_naglowek(sesja: requests.Session) -> str:
    return "; ".join("%s=%s" % (n, w) for n, w in sesja.cookies.items())


def _zapamietaj_ciasteczka(sesja: requests.Session, naglowki) -> None:
    """Set-Cookie z mostu do sloika sesji — sloik zostaje po stronie Pythona.

    Most celowo nie trzyma zadnego stanu: paginacja CBOSA zyje w sesji
    (cookie), a wspolny sloik po stronie mostu mieszalby dwa rownolegle
    przebiegi."""
    for surowe in naglowki or []:
        para, _, atrybuty = surowe.partition(";")
        if "=" not in para:
            continue
        nazwa, wartosc = (x.strip() for x in para.split("=", 1))
        if not nazwa:
            continue
        # Serwer kasuje ciasteczko, wysylajac je z Max-Age=0 albo z data
        # wygasniecia w przeszlosci. requests.Session robi to sam; tu trzeba
        # recznie, inaczej uniewazniona sesja wracalaby w kolejnych zadaniach.
        # (Parsujemy recznie, nie SimpleCookie: CBOSA wysyla wartosci ze
        # spacjami, np. pola-orzeczenia, ktore SimpleCookie po cichu gubi.)
        if not wartosc or _ciasteczko_skasowane(atrybuty):
            try:
                del sesja.cookies[nazwa]
            except KeyError:
                pass
            continue
        sesja.cookies.set(nazwa, wartosc)


def _ciasteczko_skasowane(atrybuty: str) -> bool:
    """Czy atrybuty Set-Cookie kaza je usunac. Max-Age ma pierwszenstwo przed
    Expires (RFC 6265, 5.3)."""
    max_age, expires = None, None
    for a in atrybuty.split(";"):
        klucz, _, wartosc = a.strip().partition("=")
        klucz = klucz.strip().lower()
        if klucz == "max-age":
            max_age = wartosc.strip()
        elif klucz == "expires":
            expires = wartosc.strip()
    if max_age is not None:
        try:
            return int(max_age) <= 0
        except ValueError:
            return False
    if expires:
        try:
            kiedy = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return False
        if kiedy.tzinfo is None:
            kiedy = kiedy.replace(tzinfo=timezone.utc)
        return kiedy <= datetime.now(timezone.utc)
    return False


def _przez_most(sesja: requests.Session, metoda: str, url: str, **kw) -> OdpowiedzMostu:
    """Jedno zadanie do CBOSA przez funkcje brzegowa Supabase.

    Przekierowania podazamy sami, bo most ich nie podaza (redirect: manual) —
    inaczej zgubilby ciasteczko ustawione przez pierwszy skok."""
    for _ in range(MAKS_PRZEKIEROWAN):
        zlecenie = {
            "metoda": metoda.upper(),
            "url": url,
            "agent": USER_AGENT,
            "ciasteczka": _ciasteczka_naglowek(sesja),
        }
        if kw.get("data"):
            zlecenie["dane"] = kw["data"]
        naglowki = {"x-most-klucz": MOST_KLUCZ}
        if MOST_REGION:
            naglowki["x-region"] = MOST_REGION
        r = requests.post(MOST_URL, json=zlecenie, timeout=TIMEOUT_S + 30,
                          headers=naglowki)
        if r.status_code != 200:
            raise requests.RequestException(
                "most odpowiedzial HTTP %s: %s" % (r.status_code, r.text[:200]))
        dane = r.json()
        if dane.get("blad"):
            raise requests.RequestException("most: %s" % dane["blad"])
        _zapamietaj_ciasteczka(sesja, dane.get("ciasteczka"))

        status = int(dane.get("status") or 0)
        lokalizacja = dane.get("lokalizacja") or ""
        if status in (301, 302, 303, 307, 308) and lokalizacja:
            # urljoin, nie BAZA_URL + ...: Location wzgledne bez ukosnika
            # („find?p=1") dawalo „https://orzeczenia.nsa.gov.plfind?p=1", ktore
            # most odrzucal jako obcy host. requests.Session robi to samo.
            url = urljoin(url, lokalizacja)
            metoda = "GET" if status in (301, 302, 303) else metoda
            kw = {}
            continue
        return OdpowiedzMostu(status, dane.get("tekst") or "", url)
    raise requests.RequestException("most: przekroczono %d przekierowan" % MAKS_PRZEKIEROWAN)


# ---------------------------------------------------------------------------
# HTTP z ponowieniami
# ---------------------------------------------------------------------------
def _zadanie(sesja: requests.Session, metoda: str, url: str, log_fn=None, **kw):
    for proba in range(1, MAKS_PROB_HTTP + 1):
        try:
            time.sleep(PAUZA_S)
            if MOST_URL:
                r = _przez_most(sesja, metoda, url, **kw)
            else:
                r = sesja.request(metoda, url, timeout=TIMEOUT_S, **kw)
            if r.status_code == 200:
                return r
            if log_fn:
                log_fn(f"    HTTP {r.status_code} dla {url} (proba {proba})")
        except requests.RequestException as e:
            if log_fn:
                log_fn(f"    Blad polaczenia: {e} (proba {proba})")
        if proba < MAKS_PROB_HTTP:
            time.sleep(PAUZA_PO_BLEDZIE_S * proba)
    raise BladCBOSA(f"Nie udalo sie pobrac {url} po {MAKS_PROB_HTTP} probach")


def nowa_sesja() -> requests.Session:
    s = requests.Session()
    s.headers.update(NAGLOWKI_SESJI)
    return s


# ---------------------------------------------------------------------------
# DYNAMICZNE ODKRYWANIE POL FORMULARZA
# ---------------------------------------------------------------------------
def poznaj_formularz(sesja: requests.Session, log_fn=None) -> dict:
    """
    Pobiera strone formularza i buduje mape:
      {'akcja': url, 'domyslne': {nazwa: wartosc, ...},
       'pola': {klucz_pomocy: [ {name, type, value}, ... ]}}

    Kazde pole formularza CBOSA ma obok link pomocy p3Help('klucz') —
    szukamy inputow/selectow w tym samym wierszu tabeli co link.
    """
    r = _zadanie(sesja, "GET", QUERY_URL, log_fn=log_fn)
    soup = BeautifulSoup(r.text, "lxml")

    form = soup.find("form")
    if form is None:
        raise BladCBOSA("Nie znaleziono formularza na stronie /cbo/query")

    akcja = form.get("action") or "/cbo/query"
    if akcja.startswith("/"):
        akcja = BAZA_URL + akcja

    # Wartosci domyslne wszystkich pol (hidden itd.) — wysylamy je z powrotem,
    # zeby nie zgubic pol technicznych aplikacji.
    domyslne = {}
    for inp in form.find_all("input"):
        nazwa = inp.get("name")
        if not nazwa:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                domyslne[nazwa] = inp.get("value", "on")
        elif typ not in ("submit", "button", "image"):
            domyslne[nazwa] = inp.get("value", "")
    for sel in form.find_all("select"):
        nazwa = sel.get("name")
        if not nazwa:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        domyslne[nazwa] = opt.get("value", "") if opt else ""

    # Mapa klucz_pomocy -> pola w tym samym wierszu
    pola = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"p3Help\('(\w+)'\)", a["href"])
        if not m:
            continue
        klucz = m.group(1)
        wiersz = a.find_parent("tr") or a.find_parent("li") or a.parent
        if wiersz is None:
            continue
        lista = []
        for inp in wiersz.find_all(["input", "select"]):
            nazwa = inp.get("name")
            if not nazwa:
                continue
            lista.append({
                "name":  nazwa,
                "type":  (inp.get("type") or inp.name or "text").lower(),
                "value": inp.get("value", ""),
            })
        if lista and klucz not in pola:
            pola[klucz] = lista

    if log_fn:
        log_fn(f"  Formularz rozpoznany: akcja={akcja}, pol z kluczami pomocy: {len(pola)}")
    return {"akcja": akcja, "domyslne": domyslne, "pola": pola}


def _pole_tekstowe(formularz, klucz, ktore=0):
    lista = [p for p in formularz["pola"].get(klucz, []) if p["type"] in ("text", "")]
    if len(lista) > ktore:
        return lista[ktore]["name"]
    return None


def _radio_tak(formularz, klucz):
    """
    Dla wierszy statusu (Tak/Nie) zwraca (nazwa, wartosc) PIERWSZEGO radia
    w wierszu — w formularzu CBOSA kolejnosc to zawsze Tak, potem Nie.
    """
    radia = [p for p in formularz["pola"].get(klucz, []) if p["type"] == "radio"]
    if radia:
        return radia[0]["name"], radia[0]["value"] or "on"
    # fallback: checkbox
    chk = [p for p in formularz["pola"].get(klucz, []) if p["type"] == "checkbox"]
    if chk:
        return chk[0]["name"], chk[0]["value"] or "on"
    return None, None


def zbuduj_zapytanie(formularz: dict, symbol: str, data_od: str, data_do: str,
                     tylko_z_uzasadnieniem: bool = False,
                     tylko_prawomocne: bool = False) -> dict:
    """Sklada slownik danych POST dla zapytania o dany symbol i okno dat."""
    dane = dict(formularz["domyslne"])

    n_symbol = _pole_tekstowe(formularz, "symbole")
    if not n_symbol:
        raise BladCBOSA("Nie rozpoznano pola 'symbole' w formularzu")
    dane[n_symbol] = symbol

    n_od = _pole_tekstowe(formularz, "data_orzeczenia", 0)
    n_do = _pole_tekstowe(formularz, "data_orzeczenia", 1)
    if not n_od or not n_do:
        raise BladCBOSA("Nie rozpoznano pol dat w formularzu")
    dane[n_od] = data_od
    dane[n_do] = data_do

    if tylko_z_uzasadnieniem:
        n, v = _radio_tak(formularz, "z_uzasadnieniem")
        if n:
            dane[n] = v
    if tylko_prawomocne:
        n, v = _radio_tak(formularz, "s_prawomocne")
        if n:
            dane[n] = v
    return dane


# ---------------------------------------------------------------------------
# WYSZUKIWANIE + PAGINACJA
# ---------------------------------------------------------------------------
_RE_DOC = re.compile(r"/doc/([0-9A-Fa-f]{6,})")

# Rodzaje orzeczen POMIJANE przy archiwizacji. Postanowienia (proceduralne:
# zawieszenia, odrzucenia, koszty) nie maja wartosci merytorycznej dla
# archiwum interpretacyjnego. Wyroki i uchwaly (te ostatnie o szczegolnej
# mocy - wiaza sklady orzekajace) przechodza.
RODZAJE_POMIJANE = ("postanowienie",)


def czy_pomijac_rodzaj(tytul: str) -> bool:
    """
    True, jesli tytul z listy wynikow wskazuje rodzaj orzeczenia do pominiecia.
    Tytuly CBOSA maja staly format: "SYGN - Rodzaj SAD z YYYY-MM-DD",
    wiec rodzaj sprawdzamy po separatorze ' - ', nie w calym tytule
    (sygnatura moglaby teoretycznie zawierac mylace slowo).
    """
    if not tytul:
        return False
    czesc = tytul.split(" - ", 1)
    rodzaj_i_reszta = (czesc[1] if len(czesc) > 1 else tytul).strip().lower()
    return any(rodzaj_i_reszta.startswith(r) for r in RODZAJE_POMIJANE)
_RE_LICZBA = [
    re.compile(r"Znaleziono[:\s]+([\d\s\u00a0]+)\s*orzecze", re.I),
    re.compile(r"znalezion\w+\s+dokument\w*[:\s]+([\d\s\u00a0]+)", re.I),
    re.compile(r"Ilo\w+\s+znalezionych[^\d]*([\d\s\u00a0]+)", re.I),
]


def _parsuj_liste(html: str):
    """
    Z listy wynikow wyciaga [(id, tytul), ...], laczna liczbe (lub None)
    i liczbe WYNIKOW GLOWNYCH na tej stronie.

    Lista zawiera tez odnosniki do orzeczen POWIAZANYCH (np. wyrok WSA pod
    wyrokiem NSA, <span class="powiazane">). Zbieramy je jak dotad, ale do
    stronicowania liczy sie tylko wyniki glowne: CBOSA podaje „Znaleziono N"
    i dzieli na strony po 10 wlasnie wynikow glownych. Do 26.09.2026 liczylismy
    wszystkie odnosniki razem i konczylismy za wczesnie — np. 51 wynikow
    (6 stron) i koniec po 4 stronach, bo 69 odnosnikow > 51.
    """
    soup = BeautifulSoup(html, "lxml")
    wyniki, widziane = [], set()
    glownych = 0
    for a in soup.find_all("a", href=True):
        m = _RE_DOC.search(a["href"])
        if not m:
            continue
        did = m.group(1).upper()
        if did in widziane:
            continue
        widziane.add(did)
        wyniki.append((did, a.get_text(" ", strip=True)))
        if a.find_parent("span", class_="powiazane") is None:
            glownych += 1

    tekst = soup.get_text(" ", strip=True)
    total = None
    for rx in _RE_LICZBA:
        m = rx.search(tekst)
        if m:
            try:
                total = int(re.sub(r"[\s\u00a0]", "", m.group(1)))
                break
            except ValueError:
                pass
    return wyniki, total, glownych


def szukaj(sesja: requests.Session, formularz: dict, symbol: str,
           data_od: str, data_do: str, tylko_z_uzasadnieniem=False,
           tylko_prawomocne=False, pomijaj_postanowienia=True, log_fn=None):
    """
    Wykonuje zapytanie i przewija WSZYSTKIE strony wynikow.
    Zwraca (lista_[(id, tytul)], total_hits_lub_None).

    pomijaj_postanowienia: odfiltrowuje postanowienia (proceduralne) juz na
    poziomie listy wynikow — ich strony szczegolow NIE sa pobierane.
    UWAGA: total_hits z CBOSA obejmuje takze odfiltrowane pozycje, wiec
    lista moze byc krotsza niz total — to oczekiwane, log to pokazuje.
    """
    dane = zbuduj_zapytanie(formularz, symbol, data_od, data_do,
                            tylko_z_uzasadnieniem, tylko_prawomocne)
    r = _zadanie(sesja, "POST", formularz["akcja"], log_fn=log_fn, data=dane)
    wyniki, total, glownych = _parsuj_liste(r.text)

    odfiltrowane = 0

    def _przefiltruj(pozycje):
        nonlocal odfiltrowane
        if not pomijaj_postanowienia:
            return pozycje
        ok = []
        for i, t in pozycje:
            if czy_pomijac_rodzaj(t):
                odfiltrowane += 1
            else:
                ok.append((i, t))
        return ok

    wyniki_f = _przefiltruj(wyniki)
    if log_fn:
        log_fn(f"    [strona 1] {glownych} wynikow (+{len(wyniki) - glownych} powiazanych), "
               f"po filtrze rodzaju: {len(wyniki_f)}" + (f"; razem {total}" if total is not None else ""))

    wszystkie = list(wyniki_f)
    widziane = {i for i, _ in wyniki}          # dedup po SUROWYCH id (takze pominietych)
    # Stronicowanie po WYNIKACH GLOWNYCH — tylko je liczy „Znaleziono N"
    # (patrz _parsuj_liste). Powiazane zbieramy, ale nie przesuwaja licznika.
    przewinieto = glownych
    strona = 2
    while strona <= MAKS_STRON:
        if total is not None and przewinieto >= total:
            break
        if glownych == 0:              # strona bez wynikow glownych = koniec
            break
        r = _zadanie(sesja, "GET", f"{FIND_URL}?p={strona}", log_fn=log_fn)
        wyniki, _, glownych = _parsuj_liste(r.text)
        nowe = [(i, t) for i, t in wyniki if i not in widziane]
        if not nowe:                   # strona bez nowych pozycji = koniec/petla
            break
        for i, _t in nowe:
            widziane.add(i)
        przewinieto += glownych
        nowe_f = _przefiltruj(nowe)
        wszystkie.extend(nowe_f)
        if log_fn:
            log_fn(f"    [strona {strona}] +{glownych} wynikow, +{len(nowe)} nowych pozycji "
                   f"(+{len(nowe_f)} po filtrze; przewinieto {przewinieto}"
                   + (f"/{total}" if total is not None else "") + ")")
        strona += 1

    if log_fn and odfiltrowane:
        log_fn(f"    Pominieto {odfiltrowane} postanowien (rodzaj poza archiwum).")

    return wszystkie, total


# ---------------------------------------------------------------------------
# PARSER STRONY SZCZEGOLOW /doc/<ID>
# ---------------------------------------------------------------------------
_ETYKIETY = ["Data orzeczenia", "Data wpływu", "Sąd", "Sędziowie",
             "Symbol z opisem", "Hasła tematyczne", "Sygn. powiązane",
             "Skarżony organ", "Treść wyniku", "Powołane przepisy"]


def _tekst_po_etykiecie(soup, etykieta):
    """Znajduje komorke z dokladna etykieta i zwraca tekst sasiedniej tresci."""
    for tag in soup.find_all(["td", "th", "span", "b", "strong"]):
        if tag.get_text(strip=True) == etykieta:
            wiersz = tag.find_parent("tr")
            if wiersz:
                kom = wiersz.find_all("td")
                if len(kom) >= 2:
                    return kom[-1].get_text("\n", strip=True)
            nastepny = tag.find_next_sibling()
            if nastepny:
                return nastepny.get_text("\n", strip=True)
    return ""


def _sekcja_tresci(soup, naglowek):
    """Sekcje 'Sentencja'/'Uzasadnienie' — etykieta i tresc w jednym bloku."""
    for tag in soup.find_all(["td", "span", "b", "strong", "div"]):
        t = tag.get_text(strip=True)
        if t == naglowek:
            wiersz = tag.find_parent("tr") or tag.parent
            if wiersz:
                tekst = wiersz.get_text("\n", strip=True)
                if tekst.startswith(naglowek):
                    tekst = tekst[len(naglowek):].strip()
                if len(tekst) > 20:
                    return tekst
                # tresc moze byc w nastepnym wierszu
                nast = wiersz.find_next_sibling("tr")
                if nast:
                    return nast.get_text("\n", strip=True)
    return ""


def mapuj_symbol_na_podatek(symbole_tekst: str) -> str:
    for podatek, sym in SYMBOLE_PODATKOW.items():
        if sym in (symbole_tekst or ""):
            return podatek
    return ""


def pobierz_szczegoly(sesja: requests.Session, doc_id: str, log_fn=None) -> dict:
    """Pobiera i parsuje strone /doc/<ID>. Zwraca slownik gotowy do zapisu w bazie."""
    r = _zadanie(sesja, "GET", DOC_URL.format(id=doc_id), log_fn=log_fn)
    soup = BeautifulSoup(r.text, "lxml")

    # Tytul: "I FSK 45/21 - Wyrok NSA z 2024-09-25"
    tytul = (soup.title.get_text(strip=True) if soup.title else "")
    m = re.match(r"(.+?)\s*-\s*(Wyrok|Postanowienie|Uchwa\w+)\s+(.+?)\s+z\s+(\d{4}-\d{2}-\d{2})", tytul)
    if not m:
        # Strona bez tytulu orzeczenia to nie orzeczenie: przeciazenie, prace
        # serwisowe, zmieniony szablon — CBOSA potrafi oddac to z kodem 200.
        # Wczesniej zapisywalismy wtedy pusta date i sygnature sprawy
        # POWIAZANEJ (wyroku I instancji) jako wlasna, nadpisujac dobry rekord.
        raise BladCBOSA("Strona /doc/%s nie wyglada na orzeczenie (tytul: %r)"
                        % (doc_id, tytul[:80]))
    sygnatura, rodzaj, sad_krotki, data = m.group(1), m.group(2), m.group(3), m.group(4)

    pelny_tekst = soup.get_text(" ", strip=True)
    prawomocny = "orzeczenie prawomocne" in pelny_tekst.lower()

    dane_meta = {et: _tekst_po_etykiecie(soup, et) for et in _ETYKIETY}
    sentencja    = _sekcja_tresci(soup, "Sentencja")
    uzasadnienie = _sekcja_tresci(soup, "Uzasadnienie")

    symbole = dane_meta.get("Symbol z opisem", "")
    data_orz = dane_meta.get("Data orzeczenia", "") or data
    m_data = re.search(r"\d{4}-\d{2}-\d{2}", data_orz)
    data_orz = m_data.group(0) if m_data else data

    return {
        "id":              doc_id.upper(),
        "sygnatura":       sygnatura,
        "rodzaj":          rodzaj,
        "sad":             dane_meta.get("Sąd", "") or sad_krotki,
        "data_orzeczenia": data_orz,
        "podatek":         mapuj_symbol_na_podatek(symbole),
        "symbole":         symbole,
        "hasla":           dane_meta.get("Hasła tematyczne", ""),
        "skarzony_organ":  dane_meta.get("Skarżony organ", ""),
        "tresc_wyniku":    dane_meta.get("Treść wyniku", ""),
        "prawomocny":      prawomocny,
        "sentencja":       sentencja,
        "uzasadnienie":    uzasadnienie,
        "przepisy":        dane_meta.get("Powołane przepisy", ""),
        "sygn_powiazane":  dane_meta.get("Sygn. powiązane", ""),
        "link":            DOC_URL.format(id=doc_id.upper()),
        "status_tresci":   ("KOMPLETNY" if len(uzasadnienie) > 100
                            else "OCZEKUJE_NA_UZASADNIENIE"),
    }
