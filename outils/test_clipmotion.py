"""Tests de Clip Motion — à lancer après toute modification de clipmotion.py / clipmotion_web.py :

    comfyui\\ComfyUI_windows_portable\\python_embeded\\python.exe -s outils\\test_clipmotion.py

Sans carte graphique ni appel à Claude (~15 s). Vérifie la logique (segments, risques, remèdes, NSFW, péremption,
suivi des tâches, détection des rendus gris) et l'interface web sur un serveur de test (port 8191) : pages,
syntaxe JavaScript (si Node est installé), sécurité, projet solo jetable créé puis envoyé à la corbeille.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import clipmotion as A  # noqa: E402
import clipmotion_web as W  # noqa: E402

PORT_TEST = 8191
TMP = Path(tempfile.mkdtemp(prefix="clipmotion_test_"))


def ffmpeg_lavfi(source, sortie, duree=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(duree), "-pix_fmt", "yuv420p",
                    str(sortie)], check=True)
    return sortie


class Logique(unittest.TestCase):
    def test_segments(self):
        for duree, n in ((5, 1), (6, 2), (10, 2), (15, 3), (20, 4), (30, 6)):
            c = {"prompt": "x", "type": "solo"}
            A.regler_duree(c, duree)
            self.assertEqual(A.nb_segments(c), n, duree)
        with self.assertRaises(ValueError):
            A.regler_duree({"prompt": "x"}, 45)

    def test_peremption_compatible(self):
        c = {"prompt": "x", "type": "solo"}
        self.assertFalse(A.perimee(c | {"fait": A.reglages(c)}), "un plan de 5 s ne doit pas devenir périmé")
        c2 = dict(c)
        A.regler_duree(c2, 10)
        self.assertTrue(A.perimee(c2 | {"fait": A.reglages(c)}), "changer la durée rend la vidéo périmée")

    def test_risques(self):
        r = A.risques({"prompt": "Il ouvre la bouche, une flamme jaillit", "type": "vers"})
        self.assertTrue(any("lumière près du visage" in x for x in r))
        self.assertFalse(A.risques({"prompt": "Lumière : reflets durs sur la peau bronzée", "type": "vers"}),
                         "« peau » seule = fausse alerte")
        self.assertFalse(A.risques({"prompt": "un vieux poster flou au mur", "type": "vers"}), "poster ≠ texte")
        c = {"prompt": "Il joue", "type": "solo", "duree": 10, "suites": ["Il lève la guitare puis la fracasse"]}
        self.assertTrue(any(x.startswith("segment 2 (5–10 s)") for x in A.risques(c)), "risque nommé par segment")
        self.assertFalse(A.risques({"prompt": "Elle se déshabille", "type": "solo"}, (), True), "NSFW : pas d'alerte")

    def test_remede(self):
        c = {"prompt": "dent en or brillante, visage dans l'ombre", "type": "vers"}
        self.assertIn("glowing mouth", A.remede(c))
        c["neg"] = A.remede(c)
        self.assertEqual(A.remede(c), "")
        self.assertFalse(A.risques(c))

    def test_nsfw(self):
        self.assertIn("nudity", A.negatif(False))
        self.assertNotIn("nudity", A.negatif(True))
        self.assertIn("underage", A.negatif(True), "mineurs toujours exclus en NSFW")
        self.assertEqual(A.preparer_prompt("une chambre nue", False, False), "une chambre dépouillée")
        self.assertEqual(A.preparer_prompt("une chambre nue", False, True), "une chambre nue")
        self.assertIn("mineur", A.consigne(True))
        self.assertIn("YouTube", A.consigne(False))

    def test_suivi_taches(self):
        t = W.Tache(None)
        t.prog = {"phase": "", "k": 0, "n": 0, "cid": "", "t": 0, "faites": 0, "durees": [], "post": 0}
        for l in ("2 plan(s) à générer (a, b) = 3 rendu(s) de 5 s, ~10 min estimées.", "Démarrage de ComfyUI (x)…",
                  "[1/3] a (solo, segment 1/2, 81 img, seed 42)", "  OK a segment 1/2 en 2.8 min"):
            t._suivre(l)
        self.assertEqual((t.prog["plans"], t.prog["n"], t.prog["faites"], t.prog["phase"]), (2, 3, 1, "generation"))
        self.assertTrue(t._suivre("frame= 120 fps=40"), "lignes ffmpeg non gardées dans le journal")
        t._suivre("  51 plans, 5420 frames 1920×1080@30 (h264_amf) → x.mp4")
        self.assertEqual((t.prog["phase"], t.prog["total"]), ("rendu", 5420))

    def test_video_grise(self):
        gris = ffmpeg_lavfi("color=c=gray:s=320x240:r=16", TMP / "gris.mp4")
        bruit = ffmpeg_lavfi("testsrc2=s=320x240:r=16", TMP / "mire.mp4")
        self.assertTrue(A.video_grise(gris))
        self.assertFalse(A.video_grise(bruit))


def requete(chemin, data=None, entete=True):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT_TEST}{chemin}",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers=({"X-ClipMotion": "1"} if entete else {}) | {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Interface(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        code = (f"import sys; sys.path.insert(0, {str(ICI)!r}); import clipmotion as A, clipmotion_web as W; "
                f"W.PORT = {PORT_TEST}; W.ui(A, navigateur=False)")
        cls.proc = subprocess.Popen([sys.executable, "-s", "-c", code], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                if requete("/api/ping")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("le serveur de test ne démarre pas")

    @classmethod
    def tearDownClass(cls):
        if (A.RACINE / "rendus" / "zz-autotest").exists():
            A.effacer_solo("zz-autotest")
        cls.proc.kill()

    def test_page_et_javascript(self):
        code, page = requete("/")
        self.assertEqual(code, 200)
        page = page.decode()
        self.assertIn("<title>Clip Motion</title>", page)
        node = shutil.which("node")
        if node:
            js = TMP / "page.js"
            js.write_text(page[page.index("<script>") + 8:page.rindex("</script>")], encoding="utf-8")
            r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_api(self):
        code, corps = requete("/api/projets")
        self.assertEqual(code, 200)
        self.assertIn("projets", json.loads(corps))
        self.assertEqual(requete("/logo.svg")[0], 200)
        self.assertEqual(requete("/api/lancer", {"action": "maj", "nom": "x"}, entete=False)[0], 403,
                         "requête sans en-tête refusée")
        self.assertEqual(requete("/api/lancer", {"action": "rm", "nom": "x"})[0], 400, "action hors liste refusée")

    def test_projet_solo(self):
        img = TMP / "portrait.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=480x800", "-frames:v", "1",
                        str(img)], check=True)
        import base64
        self.assertEqual(requete("/api/solo/creer", {"nom": "zz-autotest"})[0], 200)
        code, corps = requete("/api/solo/zz-autotest/ajouter",
                              {"fichier": "portrait.png", "image": "data:image/png;base64," +
                               base64.b64encode(img.read_bytes()).decode(), "prompt": "Il hoche la tête", "duree": 10})
        self.assertEqual(code, 200, corps)
        cid = json.loads(corps)["id"]
        cases = json.loads(requete("/api/cases/zz-autotest")[1])["cases"]
        self.assertEqual((cases[0]["w"], cases[0]["h"], cases[0]["nseg"]), (480, 832, 2), "portrait + 2 segments")
        self.assertEqual(requete("/api/case/zz-autotest", {"id": cid, "note": "test"})[0], 200)
        self.assertEqual(requete("/api/solo/zz-autotest/assistant", {"idee": " "})[0], 400, "idée vide refusée")
        self.assertEqual(requete("/api/solo/zz-autotest/effacer", {})[0], 200)
        self.assertFalse((A.RACINE / "rendus" / "zz-autotest").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
