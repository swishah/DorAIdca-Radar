import importlib

import streamlit as st
import time

import paleta

# Moduly funkcjonalne NIE sa importowane tutaj. Wczesniej wszystkie jedenascie
# ladowalo sie na starcie, ciagnac ze soba pandas, yfinance, reportlab i lxml —
# takze wtedy, gdy uzytkownik wchodzil tylko do Archiwum. Cold start po
# uspieniu aplikacji trwal przez to kilkanascie sekund.
#
# Teraz modul laduje sie dopiero, gdy jest wybrany w menu. Python trzyma juz
# raz zaimportowany modul w sys.modules, wiec kolejne wejscia sa darmowe —
# koszt placi sie raz, i tylko za to, z czego naprawde korzystasz.
import auth


def _modul(nazwa: str):
    """Import na zadanie. Blad importu pokazujemy wprost zamiast bialej strony."""
    try:
        return importlib.import_module(nazwa)
    except Exception as e:
        st.error(f"Nie udało się załadować modułu „{nazwa}”: {e}")
        st.stop()

# Logo dolaczone bezposrednio w kodzie (base64) - dziala niezaleznie od
# tego, gdzie i jak jest hostowana aplikacja, bez osobnego pliku obrazka.
_LOGO_B64 = (
    "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"
)


def _naglowek_logo(wysokosc_px: int = 34):
    """
    Renderuje logo + nazwe marki. W trybie ciemnym logo (ktore ma stale
    wpisane ciemne kolory w samym pliku PNG) dostaje jasna plakietke pod
    spodem, zeby napis w logo pozostal czytelny na ciemnym tle.
    """
    tryb = paleta.aktywny_tryb()
    tlo_plakietki = "#F3F1E8" if tryb == "dark" else "transparent"
    kolor_tekstu = paleta.paleta()["text"]
    st.markdown(
        f'''
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
            <div style="background:{tlo_plakietki}; border-radius:8px;
                        padding:4px 7px; display:flex; align-items:center;">
                <img src="data:image/png;base64,{_LOGO_B64}"
                     style="height:{wysokosc_px}px; display:block;">
            </div>
            <span style="font-size:{wysokosc_px * 0.62}px; font-weight:700;
                         color:{kolor_tekstu}; letter-spacing:-0.01em;">
                {paleta.NAZWA_MARKI}
            </span>
        </div>
        ''',
        unsafe_allow_html=True,
    )


# 1. Konfiguracja glownej strony
st.set_page_config(page_title=paleta.NAZWA_MARKI, page_icon="📗", layout="wide")

# 2. System autoryzacji — konto zaszyte DORADCA (awaryjny superadmin z
# Secrets) ORAZ konta bazodanowe (@doradca.lublin.pl, role admin/user).
# Konto DORADCA jest sprawdzane jako pierwsze i NIE zależy od bazy kont —
# dzięki temu zawsze można się zalogować, nawet gdyby tabela kont miała
# problem, i to nim tworzy się pierwsze konta.
import hmac
import time as _time

import sesja as _sesja

for _k, _v in {"authenticated": False, "rola": None, "user_email": None,
               "superadmin": False, "proby_logowania": 0}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# Tabela kont tworzy się sama; błąd bazy nie może zablokować logowania DORADCA.
# Guard prędkości: CREATE TABLE IF NOT EXISTS wykonuje się tylko RAZ (cache_resource),
# a nie przy każdym przeładowaniu strony — to była główna przyczyna „lagów”.
@st.cache_resource(show_spinner=False)
def _tabela_kont_gotowa() -> bool:
    auth.zapewnij_tabele()
    return True


try:
    _tabela_kont_gotowa()
except Exception:
    pass


# Komponent ciasteczek musi zdążyć odesłać dane, ZANIM zdecydujemy, czy
# pokazać ekran logowania. Bez tego pierwszy przebieg skryptu widzi pustkę
# i wyrzuca zalogowanego użytkownika mimo ważnego ciasteczka.
_sesja.przygotuj()


def _odtworz_z_ciasteczka() -> None:
    """Przywraca sesję po odświeżeniu strony, o ile ciasteczko jest ważne.

    Uprawnienia czytamy Z BAZY, a nie z tokenu. Token mówi tylko „to ten
    użytkownik” — rola i status muszą pochodzić ze świeżego odczytu, inaczej
    odebranie komuś uprawnień albo dezaktywacja konta nie zadziałałyby, dopóki
    nie wygaśnie ciasteczko.
    """
    if st.session_state["authenticated"]:
        return
    email = _sesja.odczytaj_email()
    if not email:
        return

    if email == "DORADCA":
        st.session_state.update(authenticated=True, rola="admin",
                                user_email="DORADCA", superadmin=True,
                                proby_logowania=0)
        return

    try:
        u = auth.pobierz_uzytkownika(email)
    except Exception:
        return
    if not u or u.get("status") != "aktywne":
        # Konto zniknęło albo zostało zablokowane — ciasteczko jest bezużyteczne.
        _sesja.wyczysc_sesje()
        return
    st.session_state.update(authenticated=True, rola=u["rola"],
                            user_email=email,
                            superadmin=u["rola"] == "admin",
                            proby_logowania=0)


_odtworz_z_ciasteczka()


def _zaloguj_doradca(login: str, haslo: str) -> bool:
    try:
        dobry_login = st.secrets["auth"]["login"]
        dobre_haslo = st.secrets["auth"]["haslo"]
    except Exception:
        return False
    ok = (hmac.compare_digest(login.encode(), dobry_login.encode())
          and hmac.compare_digest(haslo.encode(), dobre_haslo.encode()))
    if ok:
        st.session_state.update(authenticated=True, rola="admin",
                                user_email="DORADCA", superadmin=True,
                                proby_logowania=0)
    return ok


if not st.session_state['authenticated']:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col_login, _ = st.columns([1, 2])
    with col_login:
        _naglowek_logo(wysokosc_px=40)
        tab_log, tab_akt = st.tabs(["🔐 Logowanie", "✉️ Pierwsze logowanie / aktywacja"])

        with tab_log:
            st.caption("Login: „DORADCA” (administrator) lub adres "
                       "@doradca.lublin.pl.")
            username = st.text_input("Login / adres e-mail:", key="log_user")
            password = st.text_input("Hasło:", type="password", key="log_pass")
            zapamietaj = st.checkbox(
                "Nie wylogowuj mnie", key="log_zapamietaj",
                help=f"Zaznaczone: pozostajesz zalogowany przez "
                     f"{_sesja.DNI_DLUGIE} dni, także po zamknięciu "
                     f"przeglądarki. Odznaczone: odświeżenie strony nie "
                     f"wylogowuje, ale zamknięcie przeglądarki tak.")

            # Cicha awaria zapamiętywania jest gorsza niż brak funkcji —
            # wyglądałaby jak „znowu mnie wylogowało” bez żadnej wskazówki.
            if not _sesja.dostepne():
                st.warning(
                    "Zapamiętywanie logowania jest niedostępne — brak "
                    "biblioteki `extra-streamlit-components`. Dodaj ją do "
                    "requirements.txt i przeładuj aplikację. "
                    f"Szczegóły: {_sesja.blad_importu()}", icon="⚠️")

            if st.button("🚀 Zaloguj się", type="primary",
                         use_container_width=True, key="log_btn"):
                if st.session_state['proby_logowania'] >= 3:
                    _time.sleep(min(2 ** (st.session_state['proby_logowania'] - 2), 30))

                # 1) konto zaszyte DORADCA (niezależne od bazy)
                if _zaloguj_doradca(username.strip(), password):
                    _sesja.zapisz_sesje("DORADCA", zapamietaj)
                    st.rerun()
                else:
                    # 2) konto bazodanowe (@doradca.lublin.pl)
                    sesja = None
                    try:
                        sesja = auth.zaloguj(username.strip(), password)
                    except Exception as e:
                        st.error(f"Błąd logowania: {e}")
                    if sesja:
                        st.session_state.update(
                            authenticated=True, rola=sesja["rola"],
                            user_email=sesja["email"],
                            superadmin=sesja["superadmin"], proby_logowania=0)
                        _sesja.zapisz_sesje(sesja["email"], zapamietaj)
                        st.rerun()
                    else:
                        st.session_state['proby_logowania'] += 1
                        st.error("Błędne dane logowania albo konto nieaktywne.")

        with tab_akt:
            st.caption("Aktywacja konta utworzonego przez administratora. "
                       "Kod otrzymasz na swój adres @doradca.lublin.pl "
                       "(ważny 24 h).")
            a_email = st.text_input("Adres e-mail konta:", key="akt_email")
            a_kod = st.text_input("Kod aktywacyjny (6 cyfr):", key="akt_kod")
            a_h1 = st.text_input("Ustaw hasło:", type="password", key="akt_h1")
            a_h2 = st.text_input("Powtórz hasło:", type="password", key="akt_h2")
            st.caption("Hasło: min. 8 znaków, w tym cyfra i znak specjalny.")

            if st.button("✅ Aktywuj konto", type="primary",
                         use_container_width=True, key="akt_btn"):
                if a_h1 != a_h2:
                    st.error("Hasła nie są identyczne.")
                else:
                    try:
                        auth.aktywuj(a_email.strip(), a_kod.strip(), a_h1)
                        st.success("Konto aktywne. Przejdź do zakładki "
                                   "„Logowanie” i zaloguj się.")
                    except ValueError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Nie udało się aktywować: {e}")
    st.stop()

# 3. Glowne menu boczne (Nawigacja) — z egzekwowaniem uprawnien.
with st.sidebar:
    _naglowek_logo(wysokosc_px=28)

# Efektywna rola: superadmin/DORADCA i konta admin -> 'admin', reszta -> 'user'.
_ROLA = "admin" if (st.session_state.get("superadmin")
                     or st.session_state.get("rola") == "admin") else "user"

_MODULY = [
    ("1", "Ściągacz Interpretacji"),
    ("2", "Archiwum Interpretacji"),
    ("3", "Analiza Wskaźnikowa"),
    ("4", "Wyroki Sądów (WSA/NSA)"),
    ("5", "Zestawienie Tygodniowe (poprzednie)"),
    ("6", "Zestawienie Tygodniowe"),
    ("7", "Monitoring i Powiadomienia"),
    ("8", "Wyszukiwarka Interpretacji"),
    ("9", "Aktywność systemu"),
    ("10", "Mój panel"),
    ("11", "Ustawienia Systemu"),
    ("13", "Dostęp wtyczki"),
]

# Moduly czasowo wylaczone z menu. Kod i pliki ZOSTAJA nietkniete w repo —
# zeby przywrocic pozycje, wystarczy usunac jej numer z tego zbioru.
#
# Modul 5 nosil wczesniej nazwe „Zestawienie Tygodniowe”, ktora przejal modul 6.
# Zostawiam go w liscie pod nazwa z dopiskiem „(poprzednie)”, zeby po
# ewentualnym przywroceniu nie bylo w menu dwoch identycznych pozycji —
# st.sidebar.radio rozroznia wybory po TRESCI etykiety, wiec duplikat
# gubilby zaznaczenie.
UKRYTE_MODULY = {"1", "2", "3", "4", "5"}

# Pozycje bez uprawnien: kłódka + wyszarzenie (są widoczne, ale wejście do
# nich jest zablokowane także w routingu — dwie warstwy zabezpieczenia).
# Odznaka „nowe” przy module „Mój panel” — liczba trafień od ostatniej wizyty.
#
# Liczymy ją tylko wtedy, gdy moduł „Mój panel” jest w ogóle dostępny, i to
# leniwie: wcześniej `panel_uzytkownika` importował się na starcie WYŁĄCZNIE
# po to, żeby policzyć tę jedną liczbę przy każdym przeładowaniu strony,
# niezależnie od tego, w którym module byłeś.
_nowych = 0
if auth.ma_dostep(_ROLA, "10"):
    try:
        _nowych = _modul("panel_uzytkownika").liczba_nowych(
            st.session_state.get("user_email") or "DORADCA")
    except Exception:
        _nowych = 0

# Numeracja w menu jest CIAGLA i liczona od 1, niezaleznie od kluczy modulow.
# Klucze ("6", "7", ...) zostaja bez zmian, bo po nich chodzi mapa uprawnien
# (auth.UPRAWNIENIA) i routing — przenumerowanie ich oznaczaloby przepisanie
# uprawnien przy kazdym ukryciu modulu.
#
# Dlatego numer widoczny w etykiecie sluzy WYLACZNIE do czytania. Wybor
# rozstrzyga mapa etykieta -> klucz; wczesniej numer byl wycinany z etykiety,
# co po przenumerowaniu wskazywaloby zupelnie inny modul.
_pozycje, _dozwolone_idx, _etykieta_na_num = [], [], {}
_licznik = 0
for _num, _nazwa in _MODULY:
    if _num in UKRYTE_MODULY:
        continue
    _licznik += 1
    _odznaka = f"  🔴 {_nowych}" if (_num == "10" and _nowych) else ""
    if auth.ma_dostep(_ROLA, _num):
        _etykieta = f"{_licznik}. {_nazwa}{_odznaka}"
        _dozwolone_idx.append(len(_pozycje))
    else:
        _etykieta = f"🔒 {_licznik}. {_nazwa}"
    _pozycje.append(_etykieta)
    _etykieta_na_num[_etykieta] = _num

# Domyslnie zaznacz pierwszy DOSTEPNY modul (user nie wyląduje na kłódce).
_domyslny = _dozwolone_idx[0] if _dozwolone_idx else 0

aktywna_zakladka = st.sidebar.radio(
    "Wybierz moduł:", _pozycje, index=_domyslny)

# Klucz modulu z mapy — NIE z tresci etykiety. Etykieta pokazuje numer
# porzadkowy (1, 2, 3...), ktory nie ma nic wspolnego z kluczem modulu.
_wybrany_num = _etykieta_na_num.get(aktywna_zakladka, "")

st.sidebar.markdown("---")
_rola_opis = "Administrator" if _ROLA == "admin" else "Użytkownik"
st.sidebar.caption(f"Zalogowano: {st.session_state.get('user_email')} · {_rola_opis}")
if st.sidebar.button("🚪 Wyloguj się", use_container_width=True):
    # Ciasteczko musi zniknąć razem ze stanem sesji — inaczej odświeżenie
    # strony zalogowałoby z powrotem tego samego użytkownika.
    _sesja.wyczysc_sesje()
    st.session_state.update(authenticated=False, rola=None, user_email=None,
                            superadmin=False)
    st.rerun()

st.sidebar.caption(f"© 2026 {paleta.NAZWA_MARKI}")

# Powiadomienie o dzisiejszej aktywności — dymek raz na sesję.
try:
    _modul("aktywnosc_systemu").toast_dzis()
except Exception:
    pass

# 4. Routing — z twardą blokadą dostępu (druga warstwa).
if _wybrany_num in UKRYTE_MODULY:
    # Nie da się tu trafić przez menu, ale zapamiętany wybór z wcześniejszej
    # sesji mógłby wskazywać ukrytą pozycję. Lepszy jasny komunikat niż
    # uruchomienie modułu, który miał być wyłączony.
    st.title("Moduł niedostępny")
    st.info("Ta sekcja jest chwilowo wyłączona. Wybierz inny moduł z menu "
            "po lewej.")
    st.stop()

if not auth.ma_dostep(_ROLA, _wybrany_num):
    st.title("🔒 Brak uprawnień")
    st.warning(
        "Ta sekcja jest dostępna wyłącznie dla administratora. "
        "Wybierz inny moduł z menu po lewej."
    )
    st.stop()

# Mapa: numer modułu -> (nazwa pliku, nazwa funkcji wejściowej). Import
# dzieje się dopiero tutaj, dla JEDNEGO wybranego modułu.
_WEJSCIA = {
    "1":  ("raporty", "run_module"),
    "2":  ("eksplorator_archiwum", "run_module"),
    "3":  ("cfo_analyzer", "run_module"),
    "4":  ("eksplorator_wyrokow", "run_module"),
    "5":  ("zestawienie_tygodniowe", "pokaz_zestawienie_tygodniowe"),
    "6":  ("zestawienie_automat", "pokaz_zestawienie_automat"),
    "7":  ("monitoring_ui", "pokaz_monitoring"),
    "8":  ("wyszukiwarka_klasyfikacji", "pokaz_wyszukiwarke"),
    "9":  ("aktywnosc_systemu", "pokaz_aktywnosc"),
    "10": ("panel_uzytkownika", "pokaz_panel"),
    "11": ("ustawienia_systemu", "pokaz_ustawienia"),
    "13": ("dostep_wtyczki", "pokaz_dostep_wtyczki"),
}

_wejscie = _WEJSCIA.get(_wybrany_num)
if _wejscie:
    _nazwa_pliku, _funkcja = _wejscie
    getattr(_modul(_nazwa_pliku), _funkcja)()
