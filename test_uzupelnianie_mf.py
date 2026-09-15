# Sprawdza uzupelnianie_mf.py bez MF: podstawiony utils, bez pauz.
import json, os, runpy, shutil, sys, tempfile, time, types

SKRYPT = sys.argv[1]
time.sleep = lambda s: None

LISTA = [{"id": str(i), "sygnatura": "S%d" % i, "typ": "", "data": "2023-01-%02d" % (1 + i // 3)}
         for i in range(9)]                     # po 3 dokumenty dziennie, 1–3 stycznia
SCENARIUSZ = {}


def lista(od, do, sesja, podatek, kod, log_fn=None):
    SCENARIUSZ["kod"] = kod
    return ([], "ERROR") if SCENARIUSZ["s"] == "lista_err" else (list(reversed(LISTA)), "OK")


def tekst(id, sesja=None):
    if SCENARIUSZ["s"] == "blokada" and id == "4":
        return "", "BLOKADA"
    return ("", "BRAK_PLIKU") if id == "1" else ("tekst " + id, "OK")


class _Sesja:
    def __enter__(self): return self
    def __exit__(self, *a): return False


sys.modules["requests"] = types.SimpleNamespace(Session=_Sesja)
sys.modules["utils"] =types.SimpleNamespace(KODY_PRZEPISOW={"PIT": 29903}, PODGLAD_URL="https://x/{id}",
                                             pobierz_wszystko_z_okresu=lista, pobierz_tekst_pdf=tekst)


def uruchom(s, maks, przepis=""):
    SCENARIUSZ["s"] = s
    os.environ.update(PODATEK="pit", PRZEPIS=przepis, OD="2023-01-01", DO="2023-01-31", MAKS_DOK=str(maks))
    kat = tempfile.mkdtemp()
    os.chdir(kat)
    runpy.run_path(SKRYPT, run_name="__main__")
    with open("wynik/wynik.json", encoding="utf-8") as f:
        w = json.load(f)
    import gzip
    with gzip.open("wynik/dokumenty.json.gz", "rt", encoding="utf-8") as f:
        d = json.load(f)
    os.chdir("/")
    shutil.rmtree(kat)
    return w, d


w, d = uruchom("ok", 100)
assert (w["status"], w["pobrane"], w["brak_tresci"], w["nastepne_od"]) == ("OK", 8, 1, "2023-02-01"), w
assert [x["id"] for x in d] == ["0", "2", "3", "4", "5", "6", "7", "8"] and d[0]["podatek"] == "PIT"
assert SCENARIUSZ["kod"] == 29903

w, d = uruchom("ok", 4)                        # dzień 2 kończy się w całości, dzień 3 czeka
assert (w["status"], w["pobrane"], w["nastepne_od"]) == ("LIMIT", 5, "2023-01-03"), w

w, d = uruchom("blokada", 100, przepis="57234")
assert (w["status"], w["pobrane"], w["nastepne_od"]) == ("BLOKADA", 3, "2023-01-02"), w
assert SCENARIUSZ["kod"] == 57234

w, d = uruchom("lista_err", 100)
assert (w["status"], w["lista"], w["pobrane"], w["nastepne_od"]) == ("BLOKADA", 0, 0, "2023-01-01"), w

for zle in ({"PODATEK": "p;rm"}, {"OD": "2023-02-01"}):
    os.environ.update({"PODATEK": "PIT", "OD": "2023-01-01", "DO": "2023-01-31", **zle})
    try:
        runpy.run_path(SKRYPT, run_name="__main__")
        raise AssertionError("przyjęło złe wejście %r" % zle)
    except SystemExit:
        pass
print("uzupelnianie_mf.py: OK")
