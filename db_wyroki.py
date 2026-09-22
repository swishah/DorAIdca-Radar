"""
db_wyroki.py — Warstwa bazodanowa modulu Wyroki Sadow Administracyjnych (CBOSA).

Korzysta z klasy SupabaseDB z db_core.py (to samo polaczenie), ale ma WLASNY
schemat i WLASNA logike zapisu, bo wyroki — w przeciwienstwie do interpretacji
z Eureki — sa dokumentami ZYWYMI:
  - najpierw publikowana jest sentencja (czesto bez uzasadnienia),
  - uzasadnienie dochodzi po tygodniach/miesiacach,
  - status prawomocnosci zmienia sie w czasie (nieprawomocny -> prawomocny).

Dlatego zapis to ON CONFLICT DO UPDATE (upsert), nie DO NOTHING, a kazdy
rekord ma status cyklu zycia tresci:
  OCZEKUJE_NA_UZASADNIENIE  - jest sentencja, uzasadnienia jeszcze brak
  KOMPLETNY                 - jest uzasadnienie
  BEZ_UZASADNIENIA_TRWALE   - minal dlugi czas, uzasadnienie zapewne nie
                              powstanie (w WSA uzasadnienie wyroku
                              oddalajacego sporzadza sie tylko na wniosek)
"""

from datetime import datetime

import db_core


STATUS_OCZEKUJE  = "OCZEKUJE_NA_UZASADNIENIE"
STATUS_KOMPLETNY = "KOMPLETNY"
STATUS_TRWALY_BRAK = "BEZ_UZASADNIENIA_TRWALE"

# Po ilu dniach od daty orzeczenia rekord bez uzasadnienia uznajemy za
# trwale go pozbawiony (nie bedzie juz rewizytowany przez strumien 2).
DNI_DO_TRWALEGO_BRAKU = 270


SCHEMA_WYROKI = [
    """CREATE TABLE IF NOT EXISTS wyroki (
        id              TEXT PRIMARY KEY,          -- identyfikator z URL /doc/XXXXXXXXXX
        sygnatura       TEXT NOT NULL,
        rodzaj          TEXT DEFAULT '',           -- Wyrok / Postanowienie / Uchwala
        sad             TEXT DEFAULT '',
        data_orzeczenia TEXT NOT NULL,             -- YYYY-MM-DD
        podatek         TEXT DEFAULT '',           -- PIT/CIT/VAT/AKCYZA/PCC (mapowane z symbolu)
        symbole         TEXT DEFAULT '',           -- surowe symbole z opisem
        hasla           TEXT DEFAULT '',
        skarzony_organ  TEXT DEFAULT '',
        tresc_wyniku    TEXT DEFAULT '',           -- np. "Oddalono skarge kasacyjna"
        prawomocny      BOOLEAN DEFAULT FALSE,
        status_tresci   TEXT DEFAULT 'OCZEKUJE_NA_UZASADNIENIE',
        sentencja       TEXT DEFAULT '',
        uzasadnienie    TEXT DEFAULT '',
        przepisy        TEXT DEFAULT '',           -- powolane przepisy (tekst + publikatory)
        sygn_powiazane  TEXT DEFAULT '',           -- np. wyrok I instancji
        link            TEXT DEFAULT '',
        pobrano_pierwszy      TEXT DEFAULT '',
        aktualizacja_ostatnia TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_w1 ON wyroki(podatek, data_orzeczenia)",
    "CREATE INDEX IF NOT EXISTS idx_w2 ON wyroki(status_tresci)",
    "CREATE INDEX IF NOT EXISTS idx_w3 ON wyroki(sygnatura)",

    """CREATE TABLE IF NOT EXISTS historia_sync_wyrokow (
        id           SERIAL PRIMARY KEY,
        uruchomiono  TEXT NOT NULL,
        strumien     TEXT NOT NULL,      -- METADANE / UZASADNIENIA / PRAWOMOCNOSC
        okno_od      TEXT DEFAULT '',
        okno_do      TEXT DEFAULT '',
        podatek      TEXT DEFAULT '',
        znaleziono   INTEGER DEFAULT 0,
        nowych       INTEGER DEFAULT 0,
        zaktualizowanych INTEGER DEFAULT 0,
        status       TEXT DEFAULT 'OK',
        szczegoly    TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_hsw1 ON historia_sync_wyrokow(uruchomiono)",
]


def inicjalizuj_schemat_wyrokow(db: db_core.SupabaseDB):
    conn = db._polacz()
    try:
        with conn.cursor() as cur:
            for sql in SCHEMA_WYROKI:
                cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ZAPIS (upsert) — swiadomie ON CONFLICT DO UPDATE
# ---------------------------------------------------------------------------
def zapisz_wyrok(db: db_core.SupabaseDB, w: dict) -> str:
    """
    Upsert jednego wyroku. Zwraca 'NOWY' lub 'AKTUALIZACJA'.

    Zasady aktualizacji (zeby nigdy nie COFNAC danych):
      - uzasadnienie nadpisujemy tylko, gdy nowe jest NIEPUSTE,
      - status_tresci podnosimy tylko "w gore" (OCZEKUJE -> KOMPLETNY);
        KOMPLETNY nigdy nie wraca do OCZEKUJE,
      - prawomocny: raz TRUE, zostaje TRUE,
      - pobrano_pierwszy ustawiane raz, aktualizacja_ostatnia zawsze.
    """
    teraz = datetime.now().isoformat(timespec="seconds")

    istnieje = db.wykonaj(
        "SELECT id FROM wyroki WHERE id = %s", (w["id"],), fetch=True
    )

    db.wykonaj(
        """
        INSERT INTO wyroki (id, sygnatura, rodzaj, sad, data_orzeczenia,
                            podatek, symbole, hasla, skarzony_organ, tresc_wyniku,
                            prawomocny, status_tresci, sentencja, uzasadnienie,
                            przepisy, sygn_powiazane, link,
                            pobrano_pierwszy, aktualizacja_ostatnia)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            -- Sygnatura i data tylko niepuste, jak pozostale pola. Do 22.09.2026
            -- szly bezwarunkowo: strona CBOSA bez tytulu (prace, przeciazenie)
            -- zerowala date i wstawiala sygnature sprawy powiazanej.
            sygnatura       = CASE WHEN EXCLUDED.sygnatura <> '' THEN EXCLUDED.sygnatura ELSE wyroki.sygnatura END,
            rodzaj          = CASE WHEN EXCLUDED.rodzaj <> '' THEN EXCLUDED.rodzaj ELSE wyroki.rodzaj END,
            sad             = CASE WHEN EXCLUDED.sad <> '' THEN EXCLUDED.sad ELSE wyroki.sad END,
            data_orzeczenia = CASE WHEN EXCLUDED.data_orzeczenia <> '' THEN EXCLUDED.data_orzeczenia ELSE wyroki.data_orzeczenia END,
            podatek         = CASE WHEN EXCLUDED.podatek <> '' THEN EXCLUDED.podatek ELSE wyroki.podatek END,
            symbole         = CASE WHEN EXCLUDED.symbole <> '' THEN EXCLUDED.symbole ELSE wyroki.symbole END,
            hasla           = CASE WHEN EXCLUDED.hasla <> '' THEN EXCLUDED.hasla ELSE wyroki.hasla END,
            skarzony_organ  = CASE WHEN EXCLUDED.skarzony_organ <> '' THEN EXCLUDED.skarzony_organ ELSE wyroki.skarzony_organ END,
            tresc_wyniku    = CASE WHEN EXCLUDED.tresc_wyniku <> '' THEN EXCLUDED.tresc_wyniku ELSE wyroki.tresc_wyniku END,
            prawomocny      = (wyroki.prawomocny OR EXCLUDED.prawomocny),
            status_tresci   = CASE
                                 WHEN wyroki.status_tresci = 'KOMPLETNY' THEN 'KOMPLETNY'
                                 WHEN EXCLUDED.status_tresci = 'KOMPLETNY' THEN 'KOMPLETNY'
                                 ELSE wyroki.status_tresci
                              END,
            sentencja       = CASE WHEN EXCLUDED.sentencja <> '' THEN EXCLUDED.sentencja ELSE wyroki.sentencja END,
            uzasadnienie    = CASE WHEN EXCLUDED.uzasadnienie <> '' THEN EXCLUDED.uzasadnienie ELSE wyroki.uzasadnienie END,
            przepisy        = CASE WHEN EXCLUDED.przepisy <> '' THEN EXCLUDED.przepisy ELSE wyroki.przepisy END,
            sygn_powiazane  = CASE WHEN EXCLUDED.sygn_powiazane <> '' THEN EXCLUDED.sygn_powiazane ELSE wyroki.sygn_powiazane END,
            link            = CASE WHEN EXCLUDED.link <> '' THEN EXCLUDED.link ELSE wyroki.link END,
            aktualizacja_ostatnia = EXCLUDED.aktualizacja_ostatnia
        """,
        (
            w["id"], w.get("sygnatura", ""), w.get("rodzaj", ""), w.get("sad", ""),
            w.get("data_orzeczenia", ""), w.get("podatek", ""), w.get("symbole", ""),
            w.get("hasla", ""), w.get("skarzony_organ", ""), w.get("tresc_wyniku", ""),
            bool(w.get("prawomocny", False)), w.get("status_tresci", STATUS_OCZEKUJE),
            w.get("sentencja", ""), w.get("uzasadnienie", ""), w.get("przepisy", ""),
            w.get("sygn_powiazane", ""), w.get("link", ""),
            teraz, teraz,
        ),
    )
    return "AKTUALIZACJA" if istnieje else "NOWY"


def oznacz_trwale_braki(db: db_core.SupabaseDB) -> int:
    """
    Rekordy OCZEKUJACE starsze niz DNI_DO_TRWALEGO_BRAKU dni przelacza na
    BEZ_UZASADNIENIA_TRWALE, zeby strumien uzasadnien nie rewizytowal ich
    w nieskonczonosc. Zwraca liczbe oznaczonych.
    """
    prog = db.wykonaj("SELECT to_char(now() - interval '%s days', 'YYYY-MM-DD') AS d" % DNI_DO_TRWALEGO_BRAKU, fetch=True)
    prog_data = prog[0]["d"]
    rows = db.wykonaj(
        """UPDATE wyroki SET status_tresci = %s, aktualizacja_ostatnia = %s
           WHERE status_tresci = %s AND data_orzeczenia < %s
             -- Data jest tekstem, a pusty tekst jest "mniejszy" od kazdej daty:
             -- bez tego warunku wyrok bez daty dostawal trwaly brak od razu.
             AND data_orzeczenia ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
           RETURNING id""",
        (STATUS_TRWALY_BRAK, datetime.now().isoformat(timespec="seconds"),
         STATUS_OCZEKUJE, prog_data),
        fetch=True,
    )
    return len(rows or [])


def oznacz_prawomocne(db: db_core.SupabaseDB, ids: list) -> int:
    """Ustawia prawomocny=TRUE dla podanych id (strumien 3). Zwraca ile zmieniono."""
    if not ids:
        return 0
    zmienione = 0
    for i in range(0, len(ids), 200):
        paczka = ids[i:i + 200]
        rows = db.wykonaj(
            """UPDATE wyroki SET prawomocny = TRUE, aktualizacja_ostatnia = %s
               WHERE id = ANY(%s) AND prawomocny = FALSE RETURNING id""",
            (datetime.now().isoformat(timespec="seconds"), paczka),
            fetch=True,
        )
        zmienione += len(rows or [])
    return zmienione


# ---------------------------------------------------------------------------
# ODCZYT
# ---------------------------------------------------------------------------
def pobierz_id_wyrokow(db: db_core.SupabaseDB) -> dict:
    """Zwraca {id: status_tresci} wszystkich wyrokow — do deduplikacji i decyzji o rewizycie."""
    rows = db.wykonaj("SELECT id, status_tresci FROM wyroki", fetch=True)
    return {r["id"]: r["status_tresci"] for r in rows}


def pobierz_wyroki(db: db_core.SupabaseDB, podatek=None, rok=None, miesiac=None,
                   status_tresci=None, tylko_prawomocne=False) -> list:
    kl, pa = [], []
    if podatek:
        kl.append("podatek = %s"); pa.append(podatek)
    if rok and miesiac:
        kl.append("data_orzeczenia LIKE %s"); pa.append(f"{rok}-{miesiac:02d}-%")
    elif rok:
        kl.append("data_orzeczenia LIKE %s"); pa.append(f"{rok}-%")
    if status_tresci:
        kl.append("status_tresci = %s"); pa.append(status_tresci)
    if tylko_prawomocne:
        kl.append("prawomocny = TRUE")
    where = f"WHERE {' AND '.join(kl)}" if kl else ""
    return db.wykonaj(
        f"""SELECT id, sygnatura, rodzaj, sad, data_orzeczenia, podatek,
                   prawomocny, status_tresci, tresc_wyniku, hasla,
                   sentencja, uzasadnienie, przepisy, sygn_powiazane, link
            FROM wyroki {where}
            ORDER BY data_orzeczenia DESC LIMIT 1000""",
        pa if pa else None, fetch=True,
    )


def statystyki_wyrokow(db: db_core.SupabaseDB) -> dict:
    total = db.wykonaj("SELECT COUNT(*) AS n FROM wyroki", fetch=True)[0]["n"]
    per_pod = db.wykonaj(
        """SELECT podatek, COUNT(*) AS liczba,
                  SUM(CASE WHEN status_tresci='KOMPLETNY' THEN 1 ELSE 0 END) AS kompletne,
                  SUM(CASE WHEN status_tresci='OCZEKUJE_NA_UZASADNIENIE' THEN 1 ELSE 0 END) AS oczekujace,
                  SUM(CASE WHEN prawomocny THEN 1 ELSE 0 END) AS prawomocne,
                  MIN(data_orzeczenia) AS najstarszy, MAX(data_orzeczenia) AS najnowszy
           FROM wyroki GROUP BY podatek ORDER BY podatek""",
        fetch=True,
    )
    return {"total": total, "per_podatek": per_pod}


def zapisz_historie_sync_wyrokow(db, strumien, okno_od, okno_do, podatek,
                                  znaleziono, nowych, zaktualizowanych,
                                  status="OK", szczegoly=""):
    db.wykonaj(
        """INSERT INTO historia_sync_wyrokow
           (uruchomiono, strumien, okno_od, okno_do, podatek,
            znaleziono, nowych, zaktualizowanych, status, szczegoly)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (datetime.now().isoformat(timespec="seconds"), strumien, okno_od, okno_do,
         podatek, znaleziono, nowych, zaktualizowanych, status, szczegoly),
    )


def pobierz_id_z_sentencja(db: db_core.SupabaseDB) -> set:
    """Id wyrokow, ktore maja juz sentencje — same identyfikatory, bez tresci."""
    rows = db.wykonaj("SELECT id FROM wyroki WHERE sentencja <> ''", fetch=True)
    return {r["id"] for r in rows or []}


def pobierz_historie_sync_wyrokow(db, limit: int = 40) -> list:
    return db.wykonaj(
        "SELECT * FROM historia_sync_wyrokow ORDER BY id DESC LIMIT %s",
        (limit,), fetch=True,
    )
