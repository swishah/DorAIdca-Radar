# -*- coding: utf-8 -*-
"""
MODUŁ 6: Zestawienie Tygodniowe
===============================================================================
Renderuje tabelę (L.p. | Sygnatura | Data wydania | Temat | Streszczenie)
z zielonym oznaczeniem publikacji opóźnionych (identyczna zasada jak
w module 5). Renderer i logika tygodni pn–nd są współdzielone z modułem 5.

Źródło danych: tabela `dokumenty` (ta sama, którą zasila synchronizacja
dzienna) + `streszczenia_auto` ze streszczeniami.

WYMAGA kolumny dokumenty.pobrano_at (migracja_pobrano_at.sql).

SKĄD BIORĄ SIĘ STRESZCZENIA
  Wyłącznie z ChatGPT — wtyczka Skaner Doradca albo ręczny prompt w Custom
  GPT, przez Edge Functions gpt-*. Ten moduł ich NIE generuje; wcześniejsza
  ścieżka OpenRoutera (lista modeli + przycisk „Streść brakujące”) została
  wyłączona, bo dublowała obieg ChatGPT gorszą jakością.

  Wszystko zapisuje się pod jednym modelem kanonicznym, więc nie ma już czego
  filtrować — kolumna `zrodlo` mówi, kto zrobił dany wpis.

  Pozycje z PEŁNYM układem sekcji (kolumna `streszczenie_pelne`) są oznaczone
  rombem i można je rozwinąć pod tabelą.
===============================================================================
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

import archiwum_supabase
import pdf_zestawienie
import streszczacz_openrouter as sopen
from zestawienie_tygodniowe import (_pasek_sortowania, _pasek_stron,
                                    _tabela_html, _zapytaj_cache)

PODATKI = ["PIT", "CIT", "VAT", "AKCYZA"]
MAKS_TYGODNI = 104

# Automat streszcza WYŁĄCZNIE interpretacje wydane od tej daty włącznie
# (data_wyd >= DATA_START). Przeszłe interpretacje są pomijane — start „od teraz”,
# potem wszystko na bieżąco. Format YYYY-MM-DD; porównanie łańcuchowe jest
# poprawne, bo data_wyd jest przechowywana jako tekst ISO.
DATA_START = "2026-07-15"

# Model, pod którym zapisuje zarówno automat, jak i wtyczka przeglądarkowa.
# Musi się zgadzać ze zmienną MODEL_ZESTAWIENIA funkcji wtyczka-zapisz
# w Supabase — przy rozjeździe powstałyby dwa wiersze na dokument i moduł
# pokazywałby tylko jeden z nich.
MODEL_KANONICZNY = getattr(sopen, "MODEL_DOMYSLNY", "openrouter/free")


# ---------------------------------------------------------------------------
# BAZA
# ---------------------------------------------------------------------------
def _zapytaj(sql: str, p: tuple | None = None) -> list[dict]:
    return archiwum_supabase._get_db().wykonaj(sql, p, fetch=True)


def _wykonaj(sql: str, p: tuple | None = None) -> int:
    return archiwum_supabase._get_db().wykonaj(sql, p, fetch=False)


@st.cache_resource(show_spinner=False)
def _zapewnij_tabele() -> bool:
    _wykonaj(
        """
        CREATE TABLE IF NOT EXISTS streszczenia_auto (
            id           SERIAL PRIMARY KEY,
            dokument_id  TEXT NOT NULL,
            podatek      TEXT NOT NULL,
            model        TEXT NOT NULL,
            temat        TEXT DEFAULT '',
            streszczenie TEXT DEFAULT '',
            wygenerowano TEXT NOT NULL,
            UNIQUE (dokument_id, model)
        )
        """
    )
    # Kolumna branż (klasyfikacja treściowa) — dokładana bezpiecznie do
    # istniejącej tabeli; starsze wpisy mają '' (bez branży).
    _wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS branze TEXT DEFAULT ''"
    )
    _wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS przedmiot TEXT DEFAULT ''"
    )
    # Pełny układ z sekcjami — zapisuje go wyłącznie wtyczka przeglądarkowa.
    # Automat zostawia tu pustkę, bo jego prompt prosi tylko o zwięzłą prozę.
    _wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS "
        "streszczenie_pelne TEXT DEFAULT ''"
    )
    # Kto utworzył wpis: 'automat' albo 'wtyczka:<profil>'.
    _wykonaj(
        "ALTER TABLE streszczenia_auto ADD COLUMN IF NOT EXISTS zrodlo TEXT DEFAULT ''"
    )
    return True


# ---------------------------------------------------------------------------
# TYGODNIE
# ---------------------------------------------------------------------------
def _poniedzialek(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


@st.cache_data(ttl=1800, show_spinner=False)
def _lista_tygodni(podatek: str) -> list[str]:
    dzis = dt.date.today()
    biezacy = _poniedzialek(dzis)
    # Nie schodzimy poniżej tygodnia progu DATA_START.
    najstarsza = _poniedzialek(dt.date.fromisoformat(DATA_START))
    try:
        w = _zapytaj(
            "SELECT MIN(NULLIF(data_wyd,'')) AS m FROM dokumenty "
            "WHERE podatek=%s AND data_wyd >= %s",
            (podatek, DATA_START),
        )
        if w and w[0].get("m"):
            najstarsza = _poniedzialek(dt.date.fromisoformat(str(w[0]["m"])[:10]))
    except Exception:
        pass
    klucze, pon = [], biezacy
    while pon >= najstarsza and len(klucze) < MAKS_TYGODNI:
        klucze.append(_klucz_tygodnia(pon))
        pon -= dt.timedelta(days=7)
    return klucze


def _granice(klucz: str) -> tuple[dt.date, dt.date]:
    rok, wk = klucz.split("-W")
    pon = dt.date.fromisocalendar(int(rok), int(wk), 1)
    return pon, pon + dt.timedelta(days=6)


# ---------------------------------------------------------------------------
# ODCZYT INTERPRETACJI + STRESZCZEŃ
# ---------------------------------------------------------------------------
def _sensowne(s: str | None) -> bool:
    """Czy zapisane streszczenie nadaje się do pokazania. Korzysta ze wspólnej
    kontroli z klienta OpenRouter (odrzuca puste, surowy/ucięty JSON, zbyt
    krótkie, etykiety bezpieczeństwa/odmowy oraz angielski). Wadliwe rekordy
    są traktowane jak brakujące — można je wygenerować ponownie przyciskiem."""
    return not sopen.streszczenie_wadliwe(s)


LIMIT_WIERSZY = 50
SORT_KOLUMNY = {
    "Data wydania": "d.data_wyd",
    "Data publikacji": "d.pobrano_at",
    "Sygnatura": "d.sygnatura",
}


def _policz(podatek: str, model: str = "", tylko_pelne: bool = False) -> int:
    """Liczba pozycji do stronicowania. Przy włączonym filtrze liczymy tylko
    te z pełnym streszczeniem — inaczej paginacja pokazywałaby strony,
    na których po odfiltrowaniu nic nie zostaje."""
    if not tylko_pelne:
        r = _zapytaj_cache(
            "SELECT COUNT(*) AS n FROM dokumenty WHERE podatek=%s AND data_wyd >= %s",
            (podatek, DATA_START))
        return int(r[0]["n"]) if r else 0

    r = _zapytaj_cache(
        """SELECT COUNT(*) AS n
           FROM dokumenty d
           JOIN streszczenia_auto s
             ON s.dokument_id = d.id AND s.model = %s
           WHERE d.podatek = %s AND d.data_wyd >= %s
             AND COALESCE(s.streszczenie_pelne, '') <> ''""",
        (model, podatek, DATA_START))
    return int(r[0]["n"]) if r else 0


# Znacznik przy pozycjach, które mają pełne streszczenie. Doklejamy go do
# tematu, bo renderer tabeli (_tabela_html) jest WSPÓŁDZIELONY z modułem 5
# i dołożenie tam kolumny zmieniłoby wygląd także tamtego zestawienia.
ZNACZNIK_PELNEGO = "◆ "


def _wiersze(podatek: str, model: str, sort_kol: str, malejaco: bool,
             offset: int = 0, limit: int = 50,
             tylko_pelne: bool = False) -> list[dict]:
    """Strona wierszy do wyświetlenia (bez pełnych tekstów), z sortowaniem.
    Data publikacji = pobrano_at (data dogrania do bazy). Cache'owane."""
    kol = SORT_KOLUMNY.get(sort_kol, "d.data_wyd")
    kier = "DESC" if malejaco else "ASC"

    # Filtr wymusza złączenie wewnętrzne — interesują nas wyłącznie dokumenty
    # ze streszczeniem, więc LEFT JOIN nic by tu nie wniósł.
    zlaczenie = "JOIN" if tylko_pelne else "LEFT JOIN"
    warunek_pelne = ("AND COALESCE(s.streszczenie_pelne, '') <> ''"
                     if tylko_pelne else "")

    rows = _zapytaj_cache(
        f"""
        SELECT d.id, d.sygnatura, d.data_wyd, d.pobrano_at,
               s.temat AS s_temat, s.streszczenie AS s_streszcz,
               COALESCE(s.branze, '') AS s_branze,
               COALESCE(s.przedmiot, '') AS s_przedmiot,
               COALESCE(s.streszczenie_pelne, '') AS s_pelne,
               COALESCE(s.zrodlo, '') AS s_zrodlo
        FROM dokumenty d
        {zlaczenie} streszczenia_auto s
               ON s.dokument_id = d.id AND s.model = %s
        WHERE d.podatek = %s AND d.data_wyd >= %s
          {warunek_pelne}
        ORDER BY {kol} {kier} NULLS LAST, d.sygnatura
        LIMIT {int(limit)} OFFSET {int(offset)}
        """,
        (model, podatek, DATA_START),
    )
    rows = [dict(r) for r in rows]  # kopia — nie modyfikujemy obiektu w cache
    for r in rows:
        r["_ma"] = _sensowne(r.get("s_streszcz"))
        r["pelne"] = (r.get("s_pelne") or "").strip()
        r["zrodlo"] = (r.get("s_zrodlo") or "").strip()
        r["_ma_pelne"] = bool(r["pelne"])

        temat = (r.get("s_temat") or "") if r["_ma"] else ""
        # Romb sygnalizuje, że pod tabelą jest co rozwinąć.
        r["temat"] = (ZNACZNIK_PELNEGO + temat) if r["_ma_pelne"] and temat else temat

        r["branza"] = (r.get("s_branze") or "") if r["_ma"] else ""
        r["przedmiot"] = (r.get("s_przedmiot") or "") if r["_ma"] else ""
        r["streszczenie"] = r.get("s_streszcz") if r["_ma"] else "— (brak streszczenia)"
        r["data_publikacji"] = r.get("pobrano_at")
    return rows


def _brakujace(podatek: str, model: str) -> list[dict]:
    """Interpretacje bez sensownego streszczenia (do przycisku i licznika).
    Lekko — bez pełnych tekstów; tekst dobierany dopiero dla wsadu."""
    rows = _zapytaj_cache(
        """
        SELECT d.id, d.sygnatura, d.data_wyd, s.streszczenie AS s_streszcz
        FROM dokumenty d
        LEFT JOIN streszczenia_auto s
               ON s.dokument_id = d.id AND s.model = %s
        WHERE d.podatek = %s AND d.data_wyd >= %s
        ORDER BY d.data_wyd DESC
        """,
        (model, podatek, DATA_START),
    )
    return [r for r in rows if not _sensowne(r.get("s_streszcz"))]


# ---------------------------------------------------------------------------
# PEŁNE STRESZCZENIA
# ---------------------------------------------------------------------------
def _zrodlo_opisowo(zrodlo: str) -> str:
    z = (zrodlo or "").strip()
    if z.startswith("wtyczka"):
        profil = z.split(":", 1)[1] if ":" in z else ""
        return f"wtyczka ({profil})" if profil else "wtyczka"
    return "automat"


def _pelne_streszczenia(rekordy: list[dict]) -> None:
    """
    Rozwijane pełne streszczenia pod tabelą.

    DLACZEGO POD TABELĄ, A NIE W NIEJ
      Renderer _tabela_html jest współdzielony z modułem 5. Dołożenie tam
      kolumny albo rozwijania zmieniłoby wygląd także tamtego zestawienia,
      a ono ma pozostać nietknięte. Stąd znacznik w tabeli i treść poniżej.

    Rozwijane pokazujemy WYŁĄCZNIE dla pozycji, które faktycznie mają pełną
    wersję. Puste rozwijane przy każdym wierszu byłyby szumem — po włączeniu
    automatu większość wpisów pełnej wersji mieć nie będzie, bo jego prompt
    prosi tylko o zwięzłą prozę.
    """
    z_pelnym = [r for r in rekordy if r.get("_ma_pelne")]
    if not z_pelnym:
        return

    st.markdown("---")
    st.markdown(f"#### Pełne streszczenia ({len(z_pelnym)})")
    st.caption(
        "Powstają przy ręcznym streszczaniu z wtyczki przeglądarkowej. "
        "Automat generuje wyłącznie wersję zwięzłą, widoczną w tabeli powyżej."
    )

    for r in z_pelnym:
        temat = (r.get("temat") or "").replace(ZNACZNIK_PELNEGO, "").strip()
        naglowek = f"{r['sygnatura']} — {temat}" if temat else str(r["sygnatura"])
        with st.expander(naglowek):
            st.caption(
                f"Data wydania: {r.get('data_wyd') or '—'} · "
                f"Źródło: {_zrodlo_opisowo(r.get('zrodlo'))}"
            )
            # Treść jest markdownem z nagłówkami sekcji w gwiazdkach —
            # Streamlit wyrenderuje je pogrubieniem bez naszego udziału.
            st.markdown(r["pelne"])


# ---------------------------------------------------------------------------
# ZESTAWIENIE TYGODNIOWE PDF
# ---------------------------------------------------------------------------
def _rekordy_tygodnia(pon: dt.date, model: str, podatek: str) -> list[dict]:
    """
    Interpretacje, które POJAWIŁY SIĘ W BAZIE w danym tygodniu.

    Warunek stoi na pobrano_at, nie na data_wyd — i to jest cała istota tego
    zestawienia. MF publikuje z opóźnieniem, więc wybór po dacie wydania
    zostawiałby dziurę: interpretacja z 20 lipca dograna 5 sierpnia nie trafiłaby
    do żadnego raportu. Raport za tydzień jej wydania powstał, zanim się
    pojawiła, a późniejsze już jej nie obejmują.

    Po dacie publikacji każda trafia do dokładnie jednego zestawienia.
    """
    od, do = pdf_zestawienie.granice_tygodnia(pon)
    return _zapytaj_cache(
        """
        SELECT d.sygnatura, d.podatek, d.data_wyd, d.pobrano_at, d.link,
               COALESCE(s.temat, '')              AS temat,
               COALESCE(s.streszczenie, '')       AS streszczenie,
               COALESCE(s.streszczenie_pelne, '') AS streszczenie_pelne,
               COALESCE(s.zrodlo, '')             AS zrodlo,
               COALESCE(s.branze, '')             AS branze,
               COALESCE(s.przedmiot, '')          AS przedmiot
        FROM dokumenty d
        LEFT JOIN streszczenia_auto s
               ON s.dokument_id = d.id AND s.model = %s
        WHERE d.podatek = %s AND d.pobrano_at >= %s AND d.pobrano_at <= %s
        ORDER BY d.data_wyd DESC, d.sygnatura
        """,
        (model, podatek, od, do),
    )


def _tygodnie_do_wyboru(ile: int = 12) -> list[dt.date]:
    """Poniedziałki ostatnich tygodni, od najnowszego. Bieżący pomijamy —
    jeszcze się nie skończył, więc zestawienie byłoby niepełne."""
    biezacy = pdf_zestawienie.poniedzialek(dt.date.today())
    return [biezacy - dt.timedelta(weeks=i) for i in range(1, ile + 1)]


def _pasek_pdf(podatek: str, model: str) -> None:
    """
    Przycisk generowania zestawienia tygodniowego — NAD tabelą, w obrębie
    zakładki danego podatku.

    Świadomie osobno dla każdego podatku, a nie jedną sekcją na dole strony:
    tak wygląda praca z tym modułem. Patrzysz na PIT — chcesz zestawienie PIT,
    bez przewijania na dół i wybierania podatku po raz drugi.

    Wybór tygodnia jest tuż nad przyciskiem, bo domyślny (ostatni zakończony)
    pasuje w większości przypadków, ale nadrabianie zaległości wymaga sięgnięcia
    wstecz.
    """
    tygodnie = _tygodnie_do_wyboru()
    etykiety = {pdf_zestawienie.opis_tygodnia(p): p for p in tygodnie}

    _, srodek, _ = st.columns([1, 2, 1])
    with srodek:
        wybrana = st.selectbox(
            "Tydzień zestawienia", options=list(etykiety.keys()), index=0,
            key=f"pdf_tydzien_{podatek}", label_visibility="collapsed",
            help="Domyślnie ostatni zakończony tydzień. Bieżący pominięty — "
                 "jeszcze trwa, więc zestawienie byłoby niepełne.")
        pon = etykiety[wybrana]

        rekordy = _rekordy_tygodnia(pon, model, podatek)
        klucz_pliku = f"pdf_dane_{podatek}_{pon.isoformat()}"

        if not rekordy:
            st.button(f"Brak interpretacji {podatek} w tym tygodniu",
                      disabled=True, use_container_width=True,
                      key=f"pdf_pusty_{podatek}")
        elif st.session_state.get(klucz_pliku):
            # Po wygenerowaniu pokazujemy pobieranie zamiast przycisku —
            # inaczej łatwo kliknąć drugi raz i czekać na to samo.
            st.download_button(
                f"Pobierz zestawienie {podatek} ({len(rekordy)})",
                data=st.session_state[klucz_pliku],
                file_name=f"zestawienie_{podatek}_{pon.isoformat()}.pdf",
                mime="application/pdf",
                use_container_width=True, type="primary",
                key=f"pdf_pobierz_{podatek}",
            )
        else:
            etykieta = f"Zestawienie PDF — {podatek}, {len(rekordy)} poz."
            if st.button(etykieta, use_container_width=True,
                         key=f"pdf_generuj_{podatek}"):
                _archiwum_font_ostrzezenie()
                try:
                    st.session_state[klucz_pliku] = pdf_zestawienie.generuj(
                        [dict(r) for r in rekordy], pon, podatek=podatek)
                    st.rerun()
                except Exception as e:
                    st.error(f"Nie udało się wygenerować PDF: {e}")

        # Podpis pod przyciskiem — dwie liczby, które realnie coś znaczą.
        if rekordy:
            bez = sum(1 for r in rekordy if not _sensowne(r.get("streszczenie")))
            # Ile pozycji PRZEPADŁOBY, gdyby zestawienie wybierało po dacie
            # wydania zamiast po dacie publikacji. To miara tego, ile ratuje
            # ten mechanizm — warto ją widzieć.
            wczesniejsze = sum(1 for r in rekordy
                               if str(r.get("data_wyd") or "")[:10] < pon.isoformat())
            uwagi = []
            if bez:
                uwagi.append(f"{bez} bez streszczenia")
            if wczesniejsze:
                uwagi.append(f"{wczesniejsze} wydanych wcześniej")
            st.caption(" · ".join(uwagi) if uwagi
                       else "Wszystkie pozycje ze streszczeniem.")


def _archiwum_font_ostrzezenie() -> None:
    """Brak fontu z polskimi znakami psuje PDF po cichu — font spada na
    Helvetica, która nie ma ą/ć/ę. Lepiej powiedzieć o tym przed pobraniem."""
    try:
        import eksplorator_archiwum as ea
        ea._zarejestruj_fonty()
        if not ea._font_polski_ok:
            st.warning(
                "Nie znaleziono fontu z polskimi znakami — w PDF mogą zniknąć "
                "ą, ć, ę, ł, ń, ó, ś, ź, ż. Dodaj folder `fonts/` z plikami "
                "DejaVuSans.ttf i DejaVuSans-Bold.ttf do repozytorium.",
                icon="⚠️")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ZAKŁADKA
# ---------------------------------------------------------------------------
def _zakladka(podatek: str, model: str) -> None:
    sort_kol, malejaco = _pasek_sortowania(
        f"auto_{podatek}", list(SORT_KOLUMNY.keys()), "Data wydania")

    tylko_pelne = st.checkbox(
        "Pokaż tylko pozycje z pełnym streszczeniem",
        key=f"auto_filtr_{podatek}",
        help="Pełne streszczenia powstają przy ręcznym streszczaniu z wtyczki. "
             "Filtr pokazuje więc sprawy, którym ktoś się faktycznie przyjrzał.",
    )

    total = _policz(podatek, model, tylko_pelne)
    if total == 0:
        if tylko_pelne:
            st.info("Żadna pozycja nie ma jeszcze pełnego streszczenia. "
                    "Powstają one przy streszczaniu z wtyczki przeglądarkowej.")
        else:
            st.info("Brak interpretacji w bazie dla tego podatku "
                    f"(od {DATA_START}).")
        return

    brak = _brakujace(podatek, model)

    k1, k2 = st.columns(2)
    k1.metric("Interpretacji (wszystkie)", total)
    k2.metric("Bez streszczenia", len(brak))

    _pasek_pdf(podatek, model)
    st.markdown("")

    offset = _pasek_stron(f"auto_{podatek}", total, LIMIT_WIERSZY)
    rekordy = _wiersze(podatek, model, sort_kol, malejaco, offset,
                       LIMIT_WIERSZY, tylko_pelne)
    st.markdown(_tabela_html(rekordy), unsafe_allow_html=True)

    if any(r.get("_ma_pelne") for r in rekordy):
        st.caption(f"Znacznik **{ZNACZNIK_PELNEGO.strip()}** przy temacie oznacza, "
                   "że pozycja ma pełne streszczenie — do rozwinięcia pod tabelą.")

    _pelne_streszczenia(rekordy)

    st.caption(
        "„Data publikacji” = data dogrania do bazy (pobrania). Sortuj po niej, "
        "aby nic nie umknęło przy publikacjach opóźnionych. Streszczenia są "
        "generowane maszynowo — zawsze weryfikuj przed użyciem."
    )


# ---------------------------------------------------------------------------
# WEJŚCIE
# ---------------------------------------------------------------------------
def pokaz_zestawienie_automat() -> None:
    st.header("📊 Zestawienie tygodniowe")

    try:
        _zapewnij_tabele()
    except Exception as e:
        st.error(f"Nie udało się przygotować tabeli streszczeń: {e}")
        return

    # Streszczenia powstają wyłącznie przez ChatGPT (wtyczka / prompt ręczny)
    # i wszystkie zapisują się pod modelem kanonicznym — nie ma już czego
    # wybierać, więc lista modeli i przycisk streszczania zniknęły stąd razem
    # z całą ścieżką OpenRoutera.
    for zakladka_ui, podatek in zip(st.tabs(PODATKI), PODATKI):
        with zakladka_ui:
            _zakladka(podatek, MODEL_KANONICZNY)


if __name__ == "__main__":
    st.set_page_config(page_title="Zestawienie automat", layout="wide")
    pokaz_zestawienie_automat()
