"""Clip Motion : interface web (http://127.0.0.1:8189, locale seulement). Lancée par un double-clic sur « Clip Motion.bat ».

- Barre latérale : projets solo (une image + un prompt par plan, sans Clip Studio) et projets Clip Studio.
- Projet Clip Studio : vue d'ensemble (étapes, action principale, options) + revue des cases.
- Projet solo : ajout de plans (glisser une image, écrire le mouvement) + même revue.
- Revue : mosaïque (survol = lecture) + grande vidéo, OK / À refaire / autre graine / Claude (appel nu),
  raccourcis clavier O, R, G, ← →.
- Une tâche à la fois (un seul GPU), suivie en bas de l'écran, arrêtable (Ctrl+Pause → arrêt propre).
"""
import base64
import collections
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8189
DOSSIER_DEFAUT = Path.home() / "Documents" / "Clip FFMPEG" / "projets"
NOM_NOUVEAU = re.compile(r"[A-Za-z0-9_-]{1,60}")
NOM_EXISTANT = re.compile(r'[^\\/:*?"<>|]{1,80}')
MIN_PAR_CASE = 2.9
IMAGE_MAX = 30 * 1024 * 1024
TYPES_IMG = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class Tache:
    """Une commande Clip Motion lancée en sous-processus ; sa sortie est gardée pour la page."""

    def __init__(self, a):
        self.A, self.proc, self.titre, self.code, self.args = a, None, "", None, []
        self.lignes = collections.deque(maxlen=500)
        self.prog = {}
        self.debut = self.fin = 0.0

    def en_cours(self):
        return self.proc is not None and self.proc.poll() is None

    def lancer(self, args, titre):
        if self.en_cours():
            return False
        self.lignes.clear()
        self.titre, self.code, self.args, self.debut, self.fin = titre, None, args, time.time(), 0.0
        self.prog = {"phase": "", "k": 0, "n": 0, "cid": "", "t": time.time(), "faites": 0, "durees": [], "post": 0}
        self.lignes.append("> Clip Motion " + " ".join(args))
        self.proc = subprocess.Popen(
            [sys.executable, "-s", "-I", self.A.__file__, *args], cwd=self.A.RACINE,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        threading.Thread(target=self._lire, daemon=True).start()
        return True

    def _lire(self):
        for ligne in self.proc.stdout:
            ligne = ligne.rstrip()
            if not self._suivre(ligne):
                self.lignes.append(ligne)
        self.code = self.proc.wait()
        self.fin = time.time()

    def _suivre(self, l):
        """Déduit la phase et l'avancement des lignes affichées par clipmotion.py (gen, post, installer)."""
        p, maintenant = self.prog, time.time()
        if m := re.search(r"(\d+) plan\(s\) à générer(?:.*?= (\d+) rendu)?", l):
            p.update(plans=int(m.group(1)), n=int(m.group(2) or m.group(1)))
        elif "Démarrage de ComfyUI" in l:
            p.update(phase="demarrage", t=maintenant, demarrage=True)
        elif m := re.match(r"\[(\d+)/(\d+)\] (\S+)", l):
            p.update(phase="generation", k=int(m.group(1)), n=int(m.group(2)), cid=m.group(3), t=maintenant)
        elif m := re.match(r"\s*OK (\S+) (?:segment \S+ )?en ([\d.]+) min", l):
            p["faites"] += 1
            p["durees"].append(float(m.group(2)) * 60)
        elif re.match(r"\s*ECHEC ", l):
            p["faites"] += 1
        elif m := re.match(r"(\S+) -> videos/", l):
            p.update(phase="post", cid=m.group(1), post=p["post"] + 1)
        elif "copiée(s)" in l or "copierait" in l:
            p.update(phase="installation", cid="")
        elif l.startswith("Correction par Claude"):
            p.update(phase="claude", t=maintenant)
        elif l.startswith("Rendu Clip Studio ("):
            p.update(phase="rendu_prep", t=maintenant)
        elif m := re.search(r"(\d+) frames \d+×\d+@", l):
            p.update(phase="rendu", total=int(m.group(1)), frame=0, t=maintenant)
        elif m := re.match(r"\s*frame=\s*(\d+)", l):
            p["frame"] = int(m.group(1))
            return True  # progression ffmpeg : pas dans le journal (une ligne par seconde)
        return False

    def arreter(self):
        """Ctrl+Pause vers le sous-processus : clipmotion.py le reçoit comme un Ctrl+C (vide la file de ComfyUI,
        interrompt le rendu, arrête le ComfyUI qu'il a démarré)."""
        if self.en_cours():
            self.proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))

    def etat(self):
        return {"en_cours": self.en_cours(), "titre": self.titre, "code": self.code, "args": self.args,
                "debut": self.debut, "fin": self.fin, "maintenant": time.time(), "prog": self.prog,
                "lignes": list(self.lignes)[-200:]}


def ui(A, revue=None, navigateur=True):
    base = f"http://127.0.0.1:{PORT}/"
    cible = base + (f"#/p/{urllib.parse.quote(revue)}/revue" if revue else "")
    try:  # déjà ouverte (double-clic une 2e fois) : juste rouvrir la page
        with urllib.request.urlopen(base + "api/ping", timeout=1) as r:
            if (lambda j: j.get("clipmotion") or j.get("animer"))(json.load(r)):  # ancienne version aussi
                print(f"Interface déjà ouverte : {cible}")
                if navigateur:
                    webbrowser.open(cible)
                return
    except (OSError, ValueError):
        pass

    tache = Tache(A)
    f_config = A.RACINE / "outils" / "config.json"
    vignettes = {}  # n° → image d'un projet Clip Studio pas encore préparé

    def config():
        try:
            return A.lire(f_config)
        except (OSError, ValueError):
            return {}

    def dossier_projets():
        return Path(config().get("dossier_projets") or DOSSIER_DEFAUT)

    def rendu(nom):
        if not nom or not NOM_EXISTANT.fullmatch(nom) or nom in (".", ".."):
            return None
        d = A.RACINE / "rendus" / nom
        return d if (d / "plans.json").is_file() else None

    def stats(d, p):
        s = {k: 0 for k in ("à générer", "à refaire", "modifiée", "à revoir", "ok")}
        for cid, c in p["cases"].items():
            s[A.libelle(d, cid, c)] += 1
        cs = p["cases"]
        s.update(installees=len(p.get("installees", [])), risques=sum(bool(A.risques(c, (), p.get("nsfw"))) for c in cs.values()),
                 generees=sum((d / "brut" / f"{i}.mp4").exists() for i in cs),
                 post=sum((d / "videos" / f"{i}.mp4").exists() for i in cs),
                 retouches=sum(c["prompt"] != c.get("prompt_auto") for c in cs.values()),
                 annotees=sum(c.get("statut") == "refaire" and bool(c.get("note")) for c in cs.values()))
        return s

    def vignette_rendu(nom, p):
        cid = next((i for i, c in p["cases"].items() if c.get("image")), None)
        return f"/image/{urllib.parse.quote(nom)}/{cid}" if cid else None

    def info_clip(dos):
        try:
            cfg = A.lire(dos / "projet.json")
        except (OSError, ValueError):
            return False, "projet.json illisible", 0
        ids = {e.get("img") for e in cfg.get("timeline") or [] if "t" in e and e.get("img")}
        if not ids:
            return False, "timeline vide : préparer le projet dans Clip Studio d'abord", 0
        return True, "", len(ids)

    def projets():
        lies, solos = {}, []
        for f in sorted((A.RACINE / "rendus").glob("*/plans.json")):
            try:
                p = A.lire(f)
            except (OSError, ValueError):
                continue
            if p.get("solo"):
                solos.append(f.parent.name)
            elif p.get("projet_clip"):
                lies[str(Path(p["projet_clip"]).resolve()).lower()] = f.parent.name
        out = []
        for nom in solos:
            d, p = A.charger(nom)
            out.append({"id": nom, "nom": nom, "titre": nom, "solo": True, "pret": True, "cases": len(p["cases"]),
                        "stats": stats(d, p), "vignette": vignette_rendu(nom, p), "dossier": str(d),
                        "nsfw": bool(p.get("nsfw"))})
        dossiers, racine = [], dossier_projets()
        if racine.is_dir():
            dossiers = [x for x in sorted(racine.iterdir(), key=lambda x: x.name.lower()) if (x / "projet.json").is_file()]
        vus = set()
        for dos in dossiers + [Path(A.lire(A.RACINE / "rendus" / n / "plans.json")["projet_clip"]) for n in lies.values()]:
            cle = str(dos.resolve()).lower()
            if cle in vus:
                continue
            vus.add(cle)
            pret, raison, n = info_clip(dos) if (dos / "projet.json").is_file() else (False, "dossier introuvable", 0)
            nom = lies.get(cle)
            x = {"id": nom or "clip:" + dos.name, "titre": dos.name, "dossier": str(dos), "pret": pret,
                 "raison": raison, "cases": n, "nom": nom, "solo": False}
            if nom:
                d, p = A.charger(nom)
                x.update(stats=stats(d, p), cases=len(p["cases"]), vignette=vignette_rendu(nom, p), nsfw=bool(p.get("nsfw")),
                         cache_mo=round(A.taille_dossier(dos / "build" / "video_frames") / 1e6))
            else:
                propose = A.slug(dos.name).lower()[:40] or "clip"
                while (A.RACINE / "rendus" / propose).exists():
                    propose += "-2"
                x["nom_propose"] = propose
                img = next((f for f in sorted((dos / "cases").glob("*")) if f.suffix.lower() in TYPES_IMG), None) \
                    if (dos / "cases").is_dir() else None
                if img:
                    vignettes[len(vignettes)] = img
                    x["vignette"] = f"/api/vignette/{len(vignettes) - 1}"
            out.append(x)
        return {"dossier": str(racine), "dossier_existe": racine.is_dir(), "projets": out,
                "comfyui": A.serveur_actif(), "min_par_case": MIN_PAR_CASE}

    def resume(d, p, cid):
        c = p["cases"][cid]
        b = d / "brut" / f"{cid}.mp4"
        return {"id": cid, "type": c["type"], "duree": c["duree_max"], "nb": len(c["apparitions"]),
                "prompt": c["prompt"], "auto": c["prompt"] == c.get("prompt_auto"), "neg": c.get("neg", ""),
                "seed": c.get("seed", ""), "statut": c.get("statut", ""), "note": c.get("note", ""),
                "video": int(b.stat().st_mtime) if b.exists() else 0, "perimee": A.perimee(c),
                "finale": (d / "videos" / f"{cid}.mp4").exists(), "w": c.get("w", A.W), "h": c.get("h", A.H),
                "installee": f"{cid}.mp4" in p.get("installees", []), "risques": A.risques(c, (), p.get("nsfw")),
                "remede": A.remede(c, p.get("nsfw")),
                "duree": round(float(c.get("duree") or A.FRAMES / A.FPS_GEN), 2), "nseg": A.nb_segments(c),
                "suites": [(c.get("suites") or [])[k] if k < len(c.get("suites") or []) else ""
                           for k in range(A.nb_segments(c) - 1)]}

    def commande(data):
        """Traduit une demande de la page en arguments de clipmotion.py (liste blanche). Renvoie (args, titre) ou une erreur."""
        act, nom = data.get("action"), str(data.get("nom") or "").strip()
        if act in ("prep", "clip"):
            dos = Path(str(data.get("dossier") or ""))
            if not (dos / "projet.json").is_file():
                return "dossier de projet Clip Studio invalide"
            d = A.RACINE / "rendus" / nom
            if (d / "plans.json").is_file():
                lie = A.lire(d / "plans.json").get("projet_clip") or ""
                if not lie or str(Path(lie).resolve()).lower() != str(dos.resolve()).lower():
                    return f"le nom « {nom} » est déjà pris par un autre projet"
            elif not NOM_NOUVEAU.fullmatch(nom):
                return "nom : lettres sans accent, chiffres, - et _ seulement"
            args = [act, str(dos), "--nom", nom] + (["--fiches"] if data.get("fiches") else [])
            return args, ("Tout animer " if act == "clip" else "Préparer ") + nom
        if act == "rendu":
            if not rendu(nom):
                return "projet inconnu"
            ap = bool(data.get("apercu"))
            return ["rendu", nom] + (["--apercu"] if ap else []), f"Rendu Clip Studio {'(aperçu) ' if ap else ''}{nom}"
        if act == "diag":
            return ["diag"] + ([nom] if rendu(nom) else []), "Diagnostic"
        if not rendu(nom):
            return "projet inconnu"
        cases = A.lire(rendu(nom) / "plans.json")["cases"]
        if act == "gen":
            t = data.get("type") or ""
            if t not in ("", "vers", "fond"):
                return "type invalide"
            return ["gen", nom] + (["--type", t] if t else []), f"Générer {nom}"
        if act == "post":
            return ["post", nom] + (["--sans-interp"] if data.get("sans_interp") else []), f"Post-traitement {nom}"
        if act in ("installer", "retirer"):
            sim = bool(data.get("simulation"))
            return [act, nom] + (["--simulation"] if sim else []), \
                f"{'Installer' if act == 'installer' else 'Retirer'} {nom}" + (" (simulation)" if sim else "")
        if act == "corriger":
            ids = data.get("ids") or []
            if not isinstance(ids, list) or any(i not in cases for i in ids):
                return "case inconnue"
            return ["corriger", nom, *ids], f"Claude corrige {', '.join(ids) or 'les cases à refaire annotées'}"
        if act in ("maj", "verifier", "etat"):
            titres = {"maj": "Générer", "verifier": "Vérifier les prompts", "etat": "Tableau des cases"}
            return [act, nom], f"{titres[act]} {nom}"
        return "action inconnue"

    class Gestion(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def envoyer(self, code, corps, type_="application/json; charset=utf-8"):
            if not isinstance(corps, bytes):
                corps = json.dumps(corps, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", type_)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def erreur(self, msg, code=400):
            self.envoyer(code, {"erreur": msg})

        def fichier(self, f, type_, telecharger=None):
            taille = f.stat().st_size
            debut, fin, code = 0, taille - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    debut = int(m.group(1))
                    fin = min(int(m.group(2)), taille - 1) if m.group(2) else taille - 1
                else:
                    debut = max(0, taille - int(m.group(2)))
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", type_)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(fin - debut + 1))
            if telecharger:
                self.send_header("Content-Disposition", f'attachment; filename="{telecharger}"')
            if code == 206:
                self.send_header("Content-Range", f"bytes {debut}-{fin}/{taille}")
            self.end_headers()
            with open(f, "rb") as h:
                h.seek(debut)
                self.wfile.write(h.read(fin - debut + 1))

        def morceaux(self):
            return [urllib.parse.unquote(x) for x in self.path.split("?")[0].strip("/").split("/")]

        def do_GET(self):
            try:
                m = self.morceaux()
                if m == [""]:
                    return self.envoyer(200, PAGE.encode(), "text/html; charset=utf-8")
                if m in (["logo.svg"], ["favicon.svg"], ["favicon.ico"]):
                    return self.fichier(Path(__file__).with_name("logo.svg"), "image/svg+xml")
                if m == ["api", "ping"]:
                    return self.envoyer(200, {"clipmotion": 1})
                if m == ["api", "projets"]:
                    return self.envoyer(200, projets())
                if len(m) == 3 and m[:2] == ["api", "rendus"] and rendu(m[2]):
                    try:
                        fs = A.rendus_clip(m[2])
                    except ValueError as e:
                        return self.erreur(str(e))
                    return self.envoyer(200, {"rendus": [{"nom": x.name, "mo": round(x.stat().st_size / 1e6),
                                                          "date": int(x.stat().st_mtime), "apercu": x.name == "preview.mp4"}
                                                         for x in fs]})
                if len(m) == 3 and m[0] == "rendu" and rendu(m[1]):
                    try:
                        f = next((x for x in A.rendus_clip(m[1]) if x.name == m[2]), None)
                    except ValueError:
                        f = None
                    if f:
                        return self.fichier(f, "video/mp4", m[2] if "telecharger" in self.path else None)
                    return self.erreur("introuvable", 404)
                if m == ["api", "tache"]:
                    return self.envoyer(200, tache.etat())
                if len(m) == 2 and m[0] == "revue":  # ancienne adresse
                    self.send_response(302)
                    self.send_header("Location", f"/#/p/{urllib.parse.quote(m[1])}/revue")
                    return self.end_headers()
                if len(m) == 3 and m[:2] == ["api", "vignette"] and m[2].isdigit() and int(m[2]) in vignettes:
                    f = vignettes[int(m[2])]
                    return self.fichier(f, TYPES_IMG.get(f.suffix.lower(), "image/png"))
                if len(m) == 3 and m[:2] == ["api", "cases"] and rendu(m[2]):
                    d = rendu(m[2])
                    p = A.lire(d / "plans.json")
                    return self.envoyer(200, {"solo": bool(p.get("solo")),
                                              "cases": [resume(d, p, c) for c in p["cases"]]})
                if len(m) == 3 and m[0] in ("video", "image", "finale") and rendu(m[1]):
                    d = rendu(m[1])
                    p = A.lire(d / "plans.json")
                    if m[2] in p["cases"]:
                        if m[0] == "image":
                            f = A.ENTREES / p["cases"][m[2]].get("image", "-")
                            if f.is_file():
                                return self.fichier(f, "image/png")
                        else:
                            f = d / ("videos" if m[0] == "finale" else "brut") / f"{m[2]}.mp4"
                            if f.is_file():
                                return self.fichier(f, "video/mp4", f"{m[1]}_{m[2]}.mp4" if m[0] == "finale" else None)
                self.erreur("introuvable", 404)
            except (ConnectionError, OSError):
                pass

        def do_POST(self):
            if self.headers.get("X-ClipMotion") != "1":  # bloque les requêtes venues d'autres sites
                return self.erreur("refusé", 403)
            taille = int(self.headers.get("Content-Length", 0))
            if taille > IMAGE_MAX * 1.4:
                return self.erreur("fichier trop gros (30 Mo max)")
            try:
                data = json.loads(self.rfile.read(taille) or b"{}")
            except ValueError:
                return self.erreur("JSON invalide")
            m = self.morceaux()
            if m == ["api", "lancer"]:
                r = commande(data)
                if isinstance(r, str):
                    return self.erreur(r)
                if not tache.lancer(*r):
                    return self.erreur("une tâche est déjà en cours", 409)
                return self.envoyer(200, {"ok": True})
            if m == ["api", "arreter"]:
                tache.arreter()
                return self.envoyer(200, {"ok": True})
            if m == ["api", "config"]:
                dos = str(data.get("dossier_projets") or "").strip()
                if not Path(dos).is_dir():
                    return self.erreur("dossier introuvable")
                A.ecrire(f_config, config() | {"dossier_projets": dos})
                return self.envoyer(200, {"ok": True})
            if m == ["api", "comfyui", "arreter"]:
                if tache.en_cours():
                    return self.erreur("une tâche utilise ComfyUI", 409)
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                                "? { $_.CommandLine -match 'ComfyUI[\\\\/]main\\.py' } | "
                                "% { Stop-Process -Id $_.ProcessId -Force }"], capture_output=True)
                return self.envoyer(200, {"ok": True})
            if m == ["api", "ouvrir"] and rendu(str(data.get("nom") or "")):
                d = rendu(data["nom"])
                cible_dos = d / "videos" if (d / "videos").is_dir() else d
                if data.get("quoi") == "rendu":  # dossier build/ du projet Clip Studio (rendus)
                    try:
                        cible_dos = A.clip_studio(data["nom"])[1] / "build"
                    except ValueError as e:
                        return self.erreur(str(e))
                subprocess.Popen(["explorer", str(cible_dos)])
                return self.envoyer(200, {"ok": True})
            if len(m) == 3 and m[:2] == ["api", "cache"] and rendu(m[2]):
                try:
                    n = A.vider_cache_clip(m[2])
                except (ValueError, OSError) as e:
                    return self.erreur(f"{e} (rendu Clip Studio en cours ?)")
                return self.envoyer(200, {"mo": round(n / 1e6)})
            if len(m) == 3 and m[:2] == ["api", "nsfw"] and rendu(m[2]):
                A.regler_nsfw(m[2], bool(data.get("actif")))
                return self.envoyer(200, {"ok": True})
            if m == ["api", "solo", "creer"]:
                try:
                    A.creer_solo(str(data.get("nom") or "").strip(), bool(data.get("nsfw")))
                except ValueError as e:
                    return self.erreur(str(e))
                return self.envoyer(200, {"ok": True})
            if len(m) == 4 and m[:2] == ["api", "solo"] and rendu(m[2]):
                if m[3] == "assistant":
                    try:
                        r = A.assistant_prompt(str(data.get("idee") or ""),
                                               bool(A.lire(rendu(m[2]) / "plans.json").get("nsfw")))
                    except A.ErreurClaude as e:
                        return self.erreur(str(e))
                    return self.envoyer(200, r)
                if m[3] == "ajouter":
                    ext = Path(str(data.get("fichier") or "image.png")).suffix.lower()
                    if ext not in TYPES_IMG:
                        return self.erreur("image PNG, JPG ou WEBP attendue")
                    try:
                        brut = base64.b64decode(str(data.get("image") or "").split(",", 1)[-1], validate=True)
                    except ValueError:
                        return self.erreur("image invalide")
                    if not brut or len(brut) > IMAGE_MAX:
                        return self.erreur("image vide ou trop grosse (30 Mo max)")
                    with tempfile.TemporaryDirectory(prefix="clipmotion_img_") as tmp:
                        f = Path(tmp) / (A.slug(Path(str(data.get("fichier") or "plan")).stem).lower()[:30] + ext)
                        f.write_bytes(brut)
                        try:
                            suites = data.get("suites") or []
                            if not isinstance(suites, list):
                                return self.erreur("suites invalides")
                            cid = A.ajouter_case(m[2], str(f), data.get("prompt") or "", str(data.get("neg") or ""),
                                                 bool(data.get("fluide", True)), duree=float(data.get("duree") or 5),
                                                 suites=[str(x) for x in suites])
                        except (ValueError, TypeError) as e:
                            return self.erreur(str(e))
                    return self.envoyer(200, {"id": cid})
                if m[3] == "effacer":
                    if tache.en_cours() and m[2] in tache.args:
                        return self.erreur("une tâche travaille sur ce projet : l'arrêter d'abord", 409)
                    try:
                        n = A.effacer_solo(m[2])
                    except (ValueError, OSError) as e:
                        return self.erreur(str(e))
                    return self.envoyer(200, {"ok": True, "images": n})
                if m[3] == "supprimer":
                    if tache.en_cours():
                        return self.erreur("attendre la fin de la tâche en cours", 409)
                    try:
                        A.supprimer_case(m[2], str(data.get("id")))
                    except ValueError as e:
                        return self.erreur(str(e))
                    return self.envoyer(200, {"ok": True})
            if len(m) == 3 and m[:2] == ["api", "case"] and rendu(m[2]):
                d = rendu(m[2])
                p = A.lire(d / "plans.json")
                cid = data.get("id")
                if cid not in p["cases"]:
                    return self.erreur("case inconnue")
                ch = {}
                if "prompt" in data:
                    ch["prompt"] = str(data["prompt"]).strip() or p["cases"][cid]["prompt_auto"]
                for k in ("neg", "note"):
                    if k in data:
                        ch[k] = str(data[k]).strip() or None
                if "seed" in data:
                    s = str(data["seed"]).strip()
                    if s and not s.isdigit():
                        return self.erreur("graine = nombre entier")
                    ch["seed"] = int(s) if s else None
                if "statut" in data:
                    if data["statut"] not in ("", "ok", "refaire"):
                        return self.erreur("statut invalide")
                    ch["statut"] = data["statut"] or None
                if "duree" in data or "suites" in data:
                    c2 = dict(p["cases"][cid])
                    suites = data.get("suites")
                    if suites is not None and not isinstance(suites, list):
                        return self.erreur("suites invalides")
                    try:
                        A.regler_duree(c2, float(data.get("duree") or c2.get("duree") or 5),
                                       None if suites is None else
                                       [A.preparer_prompt(x, False, p.get("nsfw")) if str(x).strip() else "" for x in suites])
                    except (ValueError, TypeError) as e:
                        return self.erreur(str(e))
                    ch["duree"], ch["suites"] = c2.get("duree"), c2.get("suites")
                A.maj_case(d, cid, **ch)
                return self.envoyer(200, resume(d, A.lire(d / "plans.json"), cid))
            self.erreur("introuvable", 404)

    try:
        serveur = ThreadingHTTPServer(("127.0.0.1", PORT), Gestion)
    except OSError:
        sys.exit(f"Le port {PORT} est occupé par un autre logiciel.")
    print(f"Clip Motion : {cible}\nLaisser cette fenêtre ouverte (la fermer arrête l'interface et la tâche "
          f"en cours ; Ctrl+C pour quitter proprement).", flush=True)
    if navigateur:
        webbrowser.open(cible)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        if tache.en_cours():
            print("Arrêt de la tâche en cours…")
            tache.arreter()
            try:
                tache.proc.wait(30)
            except subprocess.TimeoutExpired:
                tache.proc.kill()
        print("Interface fermée.")


# ---------- page (application d'une seule page) ----------
PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Clip Motion</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{color-scheme:light;--bg:#f4f5f8;--surface:#fff;--surface2:#eef0f4;--line:#e1e4ea;--text:#191c22;--muted:#677083;
 --accent:#4c5fd5;--accent-bg:#e9ecfc;--on-accent:#fff;--ok:#2b8a3e;--ok-bg:#e5f4e9;--ko:#c92a2a;--ko-bg:#fdeaea;
 --warn:#a65d00;--warn-bg:#fff2dc;--info:#1864ab;--info-bg:#e5f0fb;--shadow:0 1px 2px rgba(16,24,40,.05),0 2px 6px rgba(16,24,40,.06);
 --r:12px;--side:300px}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0d0f13;--surface:#151820;--surface2:#1c2029;--line:#282d38;
 --text:#e7e9ee;--muted:#8b94a5;--accent:#7086ff;--accent-bg:#1d2446;--on-accent:#fff;--ok:#5bd06f;--ok-bg:#14271a;
 --ko:#ff6b6b;--ko-bg:#2d1517;--warn:#f5b83d;--warn-bg:#2b2210;--info:#5aafff;--info-bg:#12243a;--shadow:0 1px 3px rgba(0,0,0,.45)}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",system-ui,sans-serif;overflow:hidden}
a{color:inherit}
h1{font-size:22px;margin:0;line-height:1.25}h2{font-size:16px;margin:0}h3{font-size:14px;margin:0 0 4px}
.muted{color:var(--muted)}.petit{font-size:12px}.esp{flex:1}
.ic{display:inline-flex;flex:none}.ic svg{display:block}
/* barre du haut */
.top{height:56px;display:flex;align-items:center;gap:10px;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;text-decoration:none}
.logo img{display:block;border-radius:8px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:99px;background:var(--surface2);
 font-size:12px;border:1px solid var(--line);cursor:default}
.pill.clic{cursor:pointer}.pill.clic:hover{border-color:var(--ko)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.ok{background:var(--ok)}.dot.ko{background:var(--ko)}.dot.warn{background:var(--warn)}.dot.info{background:var(--info)}
/* disposition */
.app{display:grid;grid-template-columns:var(--side) minmax(0,1fr);height:calc(100vh - 56px)}
aside{background:var(--surface);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.recherche{padding:12px;border-bottom:1px solid var(--line)}
.liste{flex:1;overflow:auto;padding:6px 8px 12px}
.section{display:flex;align-items:center;gap:6px;padding:12px 8px 6px;font-size:11px;font-weight:700;letter-spacing:.06em;
 text-transform:uppercase;color:var(--muted)}
.item{display:flex;gap:10px;align-items:center;padding:7px 8px;border-radius:9px;text-decoration:none;cursor:pointer}
.item:hover{background:var(--surface2)}.item.actif{background:var(--accent-bg)}.item.eteint{opacity:.5}
.vig{width:64px;height:38px;border-radius:6px;object-fit:cover;background:var(--surface2);flex:none;display:block}
.item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .s{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center;white-space:nowrap;overflow:hidden}
.pied{border-top:1px solid var(--line);padding:10px 12px}
main{overflow:auto;min-height:0;padding:26px 30px 40px}
body.tache main{padding-bottom:110px}
.page{max-width:1500px;margin:0 auto}
/* éléments */
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:18px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--line);background:var(--surface);
 color:var(--text);border-radius:9px;padding:7px 13px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;
 white-space:nowrap;transition:background .12s,border-color .12s,filter .12s}
.btn:hover{background:var(--surface2)}.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primaire{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}.btn.primaire:hover{filter:brightness(1.1)}
.btn.grand{padding:11px 20px;font-size:15px;border-radius:11px}
.btn.vert{background:var(--ok-bg);border-color:transparent;color:var(--ok)}.btn.rouge{background:var(--ko-bg);border-color:transparent;color:var(--ko)}
.btn.danger{color:var(--ko)}.btn.icone{padding:7px}.btn.discret{background:transparent;border-color:transparent}
input,textarea,select{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:8px 10px;width:100%}
textarea{resize:vertical;line-height:1.45}
input:focus,textarea:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 45%,transparent);border-color:var(--accent)}
input[type=checkbox]{width:16px;height:16px;padding:0;accent-color:var(--accent)}
.champ{display:grid;gap:5px;margin-top:12px}.champ>span{font-weight:600;font-size:13px}.champ small{font-weight:400;color:var(--muted)}
.coche{display:flex;gap:8px;align-items:center;cursor:pointer}
.rang{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;
 background:var(--surface2);color:var(--muted);white-space:nowrap}
.badge.ok{background:var(--ok-bg);color:var(--ok)}.badge.ko{background:var(--ko-bg);color:var(--ko)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}.badge.info{background:var(--info-bg);color:var(--info)}
.tete{display:flex;gap:16px;align-items:center;margin-bottom:18px}
.tete .vig{width:120px;height:70px;border-radius:10px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:20px}
.tab{padding:9px 14px;text-decoration:none;font-weight:600;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;
 display:flex;gap:7px;align-items:center}
.tab.on{color:var(--text);border-color:var(--accent)}.tab:hover{color:var(--text)}
/* vue d'ensemble */
.etapes{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}
.etape{display:flex;gap:10px;align-items:flex-start;padding:14px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r)}
.etape .num{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:12px;flex:none;
 background:var(--surface2);color:var(--muted)}
.etape.fait .num{background:var(--ok);color:#fff}.etape.ext .num{background:var(--info-bg);color:var(--info)}
.action{display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:16px;
 background:linear-gradient(135deg,var(--accent-bg),var(--surface))}
.action .txt{flex:1;min-width:240px}
.compteurs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.compteur{display:flex;flex-direction:column;padding:10px 16px;border-radius:10px;background:var(--surface);border:1px solid var(--line);
 text-decoration:none;min-width:100px}
.compteur b{font-size:22px;line-height:1.1}.compteur:hover{border-color:var(--accent)}
.options{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.options .card{display:flex;flex-direction:column;gap:10px}.options p{margin:0}
/* revue */
.barre{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.filtres{display:flex;gap:4px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:3px}
.filtre{border:0;background:transparent;font:inherit;font-weight:600;color:var(--muted);padding:5px 10px;border-radius:7px;cursor:pointer;
 display:flex;gap:6px;align-items:center}
.filtre .k{font-size:11px;background:var(--surface2);border-radius:99px;padding:0 7px}
.filtre.on{background:var(--accent-bg);color:var(--text)}.filtre:hover{color:var(--text)}
.revue{display:grid;grid-template-columns:minmax(0,1fr) minmax(400px,560px);gap:18px;align-items:start}
.grille{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tuile{position:relative;aspect-ratio:832/480;border-radius:9px;overflow:hidden;cursor:pointer;background:var(--surface2);
 outline:2px solid transparent;outline-offset:2px;transition:outline-color .12s}
.tuile img,.tuile video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.tuile .lab{position:absolute;left:6px;bottom:6px;background:rgba(0,0,0,.65);color:#fff;font-size:11px;font-weight:600;border-radius:5px;padding:1px 7px}
.tuile .pt{position:absolute;top:7px;right:7px;width:11px;height:11px;border-radius:50%;box-shadow:0 0 0 2px rgba(0,0,0,.45)}
.pt.ok{background:var(--ok)}.pt.ko{background:var(--ko)}.pt.warn{background:var(--warn)}.pt.info{background:var(--info)}
.tuile .al{position:absolute;top:4px;left:6px;font-size:13px}
.tuile.sel{outline-color:var(--accent)}.tuile:hover{outline-color:color-mix(in srgb,var(--accent) 50%,transparent)}
.tuile.ajout{display:grid;place-items:center;border:2px dashed var(--line);background:transparent;color:var(--muted);font-weight:600;text-align:center}
.tuile.ajout:hover,.tuile.ajout.sel{border-color:var(--accent);color:var(--accent);outline-color:transparent}
.detail{position:sticky;top:0}
.dtete{display:flex;gap:10px;align-items:center;margin-bottom:12px}
.media{background:#000;border-radius:10px;overflow:hidden;display:grid;place-items:center;position:relative}
.media video,.media img{display:block;width:100%;max-height:56vh;object-fit:contain}
.media .info{position:absolute;bottom:8px;left:8px;background:rgba(0,0,0,.65);color:#fff;font-size:12px;padding:2px 9px;border-radius:6px}
.avert{display:flex;gap:8px;align-items:flex-start;background:var(--warn-bg);color:var(--warn);padding:8px 11px;border-radius:9px;margin-top:10px;font-size:13px}
.actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:12px}
details.plus{margin-top:12px}details.plus summary{cursor:pointer;color:var(--muted);font-weight:600}
kbd{font:11px Consolas,monospace;background:var(--surface2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px}
.depot{border:2px dashed var(--line);border-radius:10px;padding:18px;text-align:center;cursor:pointer;color:var(--muted);
 display:grid;gap:8px;place-items:center;min-height:150px;transition:border-color .12s,background .12s}
.depot.survol,.depot:hover{border-color:var(--accent);background:var(--accent-bg)}
.depot img{max-width:100%;max-height:230px;border-radius:8px}
.gauche{display:grid;gap:16px;align-content:start}
.assistant{background:linear-gradient(160deg,var(--accent-bg),var(--surface) 70%)}
.segs{margin:8px 0 0;padding-left:20px;display:grid;gap:6px}.segs li{line-height:1.45}
/* accueil */
.galerie{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.pcarte{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden;
 text-decoration:none;display:flex;flex-direction:column;transition:border-color .12s,transform .12s}
.pcarte:hover{border-color:var(--accent);transform:translateY(-2px)}.pcarte.eteint{opacity:.55}
.pcarte img,.pcarte .ph{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--surface2)}
.pcarte .ph{display:grid;place-items:center;color:var(--muted)}
.pcarte .pc{padding:11px 13px 13px}.pcarte .t{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcarte .s{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center}
.section .k{font-size:10px;background:var(--surface2);border-radius:99px;padding:0 7px;letter-spacing:0}
.bienvenue{max-width:900px;margin:30px auto}
.trois{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:22px 0}
.trois .card b{display:block;margin:6px 0 2px}
.grosnum{width:30px;height:30px;border-radius:9px;background:var(--accent-bg);color:var(--accent);display:grid;place-items:center;font-weight:700}
/* tâche */
.tiroir{position:fixed;left:var(--side);right:0;bottom:0;z-index:20;background:var(--surface);border-top:1px solid var(--line);
 box-shadow:0 -6px 20px rgba(0,0,0,.10)}
.tbar{display:flex;align-items:center;gap:12px;padding:11px 18px;flex-wrap:wrap}
.tuile.encours{outline-color:var(--accent)}
.tuile .enc{position:absolute;inset:0;display:grid;place-content:center;justify-items:center;gap:6px;background:rgba(10,12,20,.55);color:#fff;font-size:13px}
.tuile .enc .spin{width:22px;height:22px;border-color:rgba(255,255,255,.3);border-top-color:#fff}
.bandeau{display:flex;gap:10px;align-items:center;background:var(--accent-bg);color:var(--accent);font-weight:600;padding:9px 12px;border-radius:9px;margin-bottom:12px}
.item .spin{width:10px;height:10px;border-width:2px}
#tpct{font-size:15px;min-width:42px}
.prog{width:320px;height:9px;border-radius:99px;background:var(--surface2);overflow:hidden;flex:none}
.prog i{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .4s}
.prog.indef i{width:35%;animation:glisse 1.3s ease-in-out infinite}
@keyframes glisse{0%{margin-left:-35%}100%{margin-left:100%}}
.spin{width:16px;height:16px;border:2px solid var(--surface2);border-top-color:var(--accent);border-radius:50%;animation:tourne .8s linear infinite;flex:none}
@keyframes tourne{to{transform:rotate(360deg)}}
.journal{margin:0;padding:10px 18px 14px;max-height:34vh;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;
 background:var(--bg);border-top:1px solid var(--line)}
.toasts{position:fixed;top:66px;right:16px;display:grid;gap:8px;z-index:30}
.toast{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--accent);box-shadow:var(--shadow);
 border-radius:10px;padding:10px 14px;max-width:380px;animation:entre .2s}
.toast.ko{border-left-color:var(--ko)}.toast.ok{border-left-color:var(--ok)}
@keyframes entre{from{opacity:0;transform:translateY(-6px)}}
@media (max-width:1250px){.revue{grid-template-columns:1fr}.detail{position:static}.etapes{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:800px){body{overflow:auto}.app{grid-template-columns:1fr;height:auto}aside{max-height:45vh}
 main{padding:18px 16px 120px}.tiroir{left:0}.etapes{grid-template-columns:1fr 1fr}.prog{width:120px}}
</style></head><body>
<header class="top"><a class="logo" href="#"><img src="/logo.svg" width="30" height="30" alt="">Clip Motion</a><span class="muted petit">cases et images → vidéos (Wan 2.2)</span>
<span class="esp"></span><span id="comfy" class="pill"><span class="dot"></span>ComfyUI</span>
<button class="btn" id="bdiag"></button><button class="btn icone" id="bmaj" title="Actualiser"></button></header>
<div class="app"><aside><div class="recherche"><input id="q" placeholder="Rechercher un projet…" autocomplete="off"></div>
<div class="liste" id="liste"></div><div class="pied" id="pied"></div></aside>
<main id="vue"><div class="page muted">Chargement…</div></main></div>
<div class="tiroir" id="tiroir" hidden><div class="tbar"><span id="tic"></span><b id="ttitre"></b>
<div class="prog" id="tprog"><i></i></div><b id="tpct"></b><span id="tinfo" class="muted"></span><span class="esp"></span>
<button class="btn" id="tjour">Journal</button><button class="btn danger" id="tstop">Arrêter</button>
<button class="btn discret" id="tferme">Fermer</button></div><pre class="journal" id="tlog" hidden></pre></div>
<div class="toasts" id="toasts"></div>
<script>
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
function h(t,p,...k){const e=document.createElement(t);for(const[a,v]of Object.entries(p||{})){if(v==null||v===false)continue;
 if(a==="class")e.className=v;else if(a==="style")e.style.cssText=v;else if(a.startsWith("on"))e.addEventListener(a.slice(2),v);
 else if(a in e&&a!=="list")e[a]=v;else e.setAttribute(a,v)}
 for(const x of k.flat(9))if(x!=null&&x!==false)e.append(x.nodeType?x:String(x));return e}
const IC={play:"M7 5l12 7-12 7z",check:"M5 12l5 5L20 7",x:"M6 6l12 12M18 6L6 18",refresh:"M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
 spark:"M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 17l.7 1.8 1.8.7-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7z",
 film:"M4 5h16v14H4zM8 5v14M16 5v14M4 9.5h4M4 14.5h4M16 9.5h4M16 14.5h4",dice:"M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5",
 undo:"M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3",pulse:"M3 12h4l3-8 4 16 3-8h4",chevL:"M15 18l-6-6 6-6",chevR:"M9 18l6-6-6-6",
 trash:"M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",plus:"M12 5v14M5 12h14",folder:"M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
 down:"M12 4v12M7 11l5 5 5-5M5 20h14",install:"M12 3v12M7 10l5 5 5-5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4",
 alert:"M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
 gear:"M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z",
 image:"M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01",stop:"M7 7h10v10H7z"};
function ic(n,o={}){const s=document.createElement("span");s.className="ic";const z=o.taille||16;
 s.innerHTML=`<svg viewBox="0 0 24 24" width="${z}" height="${z}" fill="${o.plein?"currentColor":"none"}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${IC[n]}"/></svg>`;return s}
const B=(txt,o={})=>h("button",{class:"btn "+(o.cls||""),title:o.titre,onclick:o.fn,disabled:o.off,type:"button"},o.ic&&ic(o.ic,{plein:o.plein}),txt||null);
async function api(u,corps){const r=await fetch(u,corps?{method:"POST",headers:{"Content-Type":"application/json","X-ClipMotion":"1"},
 body:JSON.stringify(corps)}:{});const t=await r.text();let j;try{j=JSON.parse(t)}catch(e){j={erreur:t}}
 if(!r.ok)throw new Error(j.erreur||t);return j}
function toast(msg,cls=""){const t=h("div",{class:"toast "+cls,textContent:msg});$("#toasts").append(t);setTimeout(()=>t.remove(),cls==="ko"?7000:4000)}
const ETATS={gen:{t:"à générer",c:"info"},modif:{t:"modifiée",c:"info"},refaire:{t:"à refaire",c:"ko"},revoir:{t:"à revoir",c:"warn"},ok:{t:"validée",c:"ok"}};
function etat(c){if(!c.video)return"gen";if(c.statut==="refaire")return"refaire";if(c.perimee)return"modif";return c.statut==="ok"?"ok":"revoir"}
const S={P:null,T:null,cases:{},solo:{},sel:{},filtre:{},q:"",fermeDebut:null,journal:false,avant:false,ajout:null};
const MIN=n=>Math.round(n*((S.P&&S.P.min_par_case)||2.9)+2);
const enc=encodeURIComponent;
function route(){const m=location.hash.match(/^#\/p\/([^/]+)(?:\/(\w+))?/);return m?{id:decodeURIComponent(m[1]),onglet:m[2]||""}:{}}
function projet(id){return S.P&&S.P.projets.find(x=>x.id===id)}
function occupe(){if(S.T&&S.T.en_cours){toast("Une tâche est déjà en cours : attends la fin ou arrête-la.","ko");return true}return false}
/* Pendant une génération : chaque case terminée apparaît aussitôt (barre latérale, revue, vue d'ensemble),
   sans perdre ce qu'on est en train d'écrire dans la fiche d'une case. */
async function majPendant(T){await rafraichirDiscret();const nom=T.args[1];if(!nom||nom!==route().id)return;
 const ecrit=document.activeElement&&document.activeElement.closest&&document.activeElement.closest("#detail,input,textarea,select");
 if($("#revue")){try{const r=await api("/api/cases/"+enc(nom));S.cases[nom]=r.cases;rendreRevue(!!ecrit);if(!ecrit)return;
   const c=r.cases.find(x=>x.id===S.sel[nom]);if(c&&c.video&&!$("#detail video"))toast("Vidéo de "+c.id+" prête : rouvre la case pour la voir","ok")}catch(e){}}
 else if(!ecrit)rendre()}
async function lancer(action,o){if(occupe())return;try{await api("/api/lancer",{action,...o});S.fermeDebut=null;majTache()}catch(e){toast(e.message,"ko")}}

/* ---------- barre latérale ---------- */
function resumeProjet(x){if(!x.nom)return x.pret?["","pas encore animé · "+x.cases+" cases"]:["",x.raison.split(" :")[0]];
 const s=x.stats,n=s["à générer"]+s["modifiée"];if(!x.cases)return["","vide : ajouter un plan"];
 if(n)return["info",n+" à générer"];if(s["à refaire"])return["ko",s["à refaire"]+" à refaire"];
 if(s["à revoir"])return["warn",s["à revoir"]+" à revoir"];return["ok",x.solo?"terminé":"terminé · "+s.installees+" installées"]}
function itemProjet(x,actif){let [c,t]=resumeProjet(x);const tourne=S.T&&S.T.en_cours&&x.nom&&S.T.args[1]===x.nom;
 if(tourne)return h("a",{class:"item"+(actif?" actif":""),href:"#/p/"+enc(x.id)},
  x.vignette?h("img",{class:"vig",src:x.vignette,loading:"lazy",alt:""}):h("div",{class:"vig"}),
  h("div",{style:"min-width:0"},h("div",{class:"t",textContent:x.titre}),h("div",{class:"s",style:"color:var(--accent)"},h("span",{class:"spin"}),"en cours…")));
 return h("a",{class:"item"+(actif?" actif":"")+(x.pret||x.nom?"":" eteint"),href:"#/p/"+enc(x.id)},
  x.vignette?h("img",{class:"vig",src:x.vignette,loading:"lazy",alt:""}):h("div",{class:"vig",style:"display:grid;place-items:center;color:var(--muted)"},ic("image")),
  h("div",{style:"min-width:0"},h("div",{class:"t",textContent:x.titre}),h("div",{class:"s"},h("span",{class:"dot "+c}),t)))}
function rendreSide(){if(!S.P)return;const q=S.q.toLowerCase(),r=route(),L=$("#liste");
 const f=S.P.projets.filter(x=>x.titre.toLowerCase().includes(q));const solos=f.filter(x=>x.solo),clips=f.filter(x=>!x.solo);
 const anim=clips.filter(x=>x.nom),autres=clips.filter(x=>!x.nom);
 L.replaceChildren(...[
  h("div",{class:"section"},"Clip Studio · animés",h("span",{class:"k"},String(anim.length))),anim.map(x=>itemProjet(x,r.id===x.id)),
  !anim.length?h("div",{class:"muted petit",style:"padding:4px 8px"},"Aucun pour l'instant."):null,
  h("div",{class:"section"},"Clip Studio · à animer",h("span",{class:"k"},String(autres.length))),autres.map(x=>itemProjet(x,r.id===x.id)),
  h("div",{class:"section"},"Projets solo",h("span",{class:"esp"}),h("a",{class:"btn discret icone",href:"#/p/"+enc("+solo"),title:"Nouveau projet solo"},ic("plus"))),
  solos.map(x=>itemProjet(x,r.id===x.id)),h("a",{class:"item"+(r.id==="+solo"?" actif":""),href:"#/p/"+enc("+solo")},
   h("div",{class:"vig",style:"display:grid;place-items:center;color:var(--accent)"},ic("plus")),h("div",{class:"s"},"Nouveau : une image + un mouvement")),
  !f.length?h("div",{class:"muted petit",style:"padding:10px"},"Aucun projet trouvé."):null].flat().filter(Boolean));
 const c=$("#comfy");c.className="pill"+(S.P.comfyui?" clic":"");c.title=S.P.comfyui?"Cliquer pour arrêter ComfyUI (libère la carte graphique)":"ComfyUI démarre tout seul quand il faut générer";
 c.replaceChildren(h("span",{class:"dot "+(S.P.comfyui?"ok":"")}),S.P.comfyui?"ComfyUI actif":"ComfyUI arrêté")}
function rendrePied(){const p=$("#pied");const inp=h("input",{value:S.P?S.P.dossier:"",placeholder:"C:\\…\\Clip FFMPEG\\projets"});
 p.replaceChildren(h("details",{},h("summary",{class:"muted petit",style:"cursor:pointer;display:flex;gap:6px;align-items:center"},ic("gear",{taille:14}),"Dossier des projets Clip Studio"),
  h("div",{class:"rang",style:"margin-top:8px;flex-wrap:nowrap"},inp,B("OK",{fn:async()=>{try{await api("/api/config",{dossier_projets:inp.value});toast("Dossier enregistré","ok");charger()}catch(e){toast(e.message,"ko")}}}))))}

/* ---------- vues ---------- */
function rendre(){if(!S.P)return;rendreSide();const r=route(),M=$("#vue");
 if(r.id==="+solo"){M.replaceChildren(vueNouveauSolo());return}
 const x=r.id&&projet(r.id);
 if(!x){M.replaceChildren(vueBienvenue());return}
 if(!x.nom){M.replaceChildren(vueNouveauClip(x));return}
 const onglet=x.solo?"revue":(r.onglet||"apercu");
 M.replaceChildren(vueProjet(x,onglet));if(onglet==="revue")chargerRevue(x)}
function carteProjet(x){const [c,t]=resumeProjet(x),E=enc(x.id);
 const s=x.stats||{},aGen=x.nom?s["à générer"]+s["à refaire"]+s["modifiée"]:0;
 let act=null;
 if(!x.nom&&x.pret)act=B("Tout animer · ~"+MIN(x.cases)+" min",{cls:"primaire",ic:"play",plein:1,fn:e=>{e.preventDefault();if(confirm("Animer les "+x.cases+" cases de « "+x.titre+" » ? (~"+MIN(x.cases)+" min, la carte graphique sera occupée)"))lancer("clip",{dossier:x.dossier,nom:x.nom_propose})}});
 else if(x.nom&&aGen)act=B("Générer "+aGen+" · ~"+MIN(aGen)+" min",{cls:"primaire",ic:"play",plein:1,fn:e=>{e.preventDefault();lancer("maj",{nom:x.nom})}});
 else if(x.nom&&s["à revoir"])act=h("a",{class:"btn",href:"#/p/"+E+"/revue"},ic("film"),"Revoir "+s["à revoir"]);
 return h("a",{class:"pcarte"+(x.pret||x.nom?"":" eteint"),href:"#/p/"+E},
  x.vignette?h("img",{src:x.vignette,loading:"lazy",alt:""}):h("div",{class:"ph"},ic("image",{taille:26})),
  h("div",{class:"pc"},h("div",{class:"rang",style:"flex-wrap:nowrap"},h("div",{class:"t",textContent:x.titre}),x.nsfw?h("span",{class:"badge ko"},"NSFW"):null),h("div",{class:"s"},h("span",{class:"dot "+c}),t),act?h("div",{class:"rang",style:"margin-top:8px"},act):null))}
function vueBienvenue(){const P=S.P.projets,anim=P.filter(x=>!x.solo&&x.nom),autres=P.filter(x=>!x.solo&&!x.nom),solos=P.filter(x=>x.solo);
 const bloc=(titre,l,vide)=>[h("div",{class:"rang",style:"margin:26px 0 10px"},h("h2",{},titre),h("span",{class:"badge"},String(l.length))),
  l.length?h("div",{class:"galerie"},l.map(carteProjet)):h("p",{class:"muted"},vide)];
 return h("div",{class:"page"},
  h("div",{class:"rang"},h("div",{},h("h1",{},"Tes projets"),h("div",{class:"muted"},"Clip Studio : "+S.P.dossier)),h("span",{class:"esp"}),
   h("a",{class:"btn primaire",href:"#/p/"+enc("+solo")},ic("plus"),"Nouveau projet solo")),
  !S.P.dossier_existe?h("div",{class:"avert"},ic("alert"),h("span",{},"Ce dossier n'existe pas : indique celui de tes projets Clip Studio en bas de la barre latérale.")):null,
  bloc("Clip Studio · animés",anim,"Aucun projet animé pour l'instant."),
  bloc("Clip Studio · à animer",autres,S.P.dossier_existe?"Tous tes projets sont animés.":"—"),
  bloc("Projets solo",solos,"Un projet solo = une image de départ + le mouvement voulu, sans Clip Studio."))}
function vueNouveauSolo(){const nom=h("input",{placeholder:"ex. essais-chat",value:""}),ns=h("input",{type:"checkbox"});
 const creer=async()=>{try{await api("/api/solo/creer",{nom:nom.value.trim(),nsfw:ns.checked});await charger();location.hash="#/p/"+enc(nom.value.trim())}catch(e){toast(e.message,"ko")}};
 nom.addEventListener("keydown",e=>{if(e.key==="Enter")creer()});setTimeout(()=>nom.focus(),0);
 return h("div",{class:"page",style:"max-width:620px"},h("h1",{},"Nouveau projet solo"),
  h("p",{class:"muted"},"Un projet solo regroupe des plans indépendants de Clip Studio : pour chacun, tu donnes l'image de départ et tu décris le mouvement. Portrait, paysage ou carré : le format est détecté tout seul."),
  h("div",{class:"card"},h("label",{class:"champ",style:"margin:0"},h("span",{},"Nom du projet",h("small",{}," (lettres sans accent, chiffres, - et _)")),nom),
   h("label",{class:"coche",style:"margin-top:12px"},ns,h("span",{},"Contenu adulte (NSFW) ",h("small",{class:"muted"},"— nudité permise, mineurs toujours exclus, pas pour YouTube ; modifiable plus tard"))),
   h("div",{class:"rang",style:"margin-top:14px"},B("Créer le projet",{cls:"primaire",ic:"plus",fn:creer}))))}
function vueNouveauClip(x){const page=h("div",{class:"page",style:"max-width:900px"});
 page.append(h("div",{class:"tete"},x.vignette?h("img",{class:"vig",src:x.vignette}):null,h("div",{},h("h1",{textContent:x.titre}),h("div",{class:"muted petit",textContent:x.dossier}))));
 if(!x.pret){page.append(h("div",{class:"card"},h("b",{},"Pas encore prêt"),h("p",{class:"muted",style:"margin:4px 0 0"},x.raison)));return page}
 const nom=h("input",{value:x.nom_propose});const fi=h("input",{type:"checkbox"});
 page.append(h("div",{class:"card action"},h("div",{class:"txt"},h("h2",{},x.cases+" cases à animer"),
   h("div",{class:"muted"},"Prépare les prompts, génère toutes les vidéos, les post-traite et les installe dans le projet Clip Studio. Environ "+MIN(x.cases)+" min.")),
   B("Tout animer",{cls:"primaire grand",ic:"play",plein:1,fn:()=>lancer("clip",{dossier:x.dossier,nom:nom.value,fiches:fi.checked})})),
  h("div",{class:"options"},h("div",{class:"card"},h("h3",{},"Réglages"),
    h("label",{class:"champ",style:"margin:0"},h("span",{},"Nom du dossier de travail",h("small",{}," dans rendus\\")),nom),
    h("label",{class:"coche",title:"Ajoute la description des personnages dans chaque prompt : fait souvent changer les vêtements en cours de mouvement"},fi,"Fiches des personnages dans les prompts (déconseillé)")),
   h("div",{class:"card"},h("h3",{},"Relire avant de générer ?"),h("p",{class:"muted petit"},"Prépare seulement la liste des cases et leurs prompts : tu les relis dans la revue, puis tu lances la génération."),
    h("div",{},B("Préparer seulement",{fn:()=>lancer("prep",{dossier:x.dossier,nom:nom.value,fiches:fi.checked})})))));
 return page}
function vueProjet(x,onglet){const E=enc(x.id),s=x.stats,page=h("div",{class:"page"});
 page.append(h("div",{class:"tete"},x.vignette?h("img",{class:"vig",src:x.vignette}):null,
  h("div",{style:"min-width:0"},h("div",{class:"rang"},h("h1",{textContent:x.titre}),h("span",{class:"badge "+(x.solo?"info":"")},x.solo?"solo":"Clip Studio"),
    x.nsfw?h("span",{class:"badge ko",title:"nudité permise (mineurs toujours exclus) : pas pour YouTube"},"NSFW"):null,
    h("label",{class:"coche petit muted",style:"margin-left:6px",title:"Non (défaut) : la nudité est exclue, pour YouTube. Oui : la nudité n'est plus exclue ; tout personnage mineur reste exclu."},
     h("input",{type:"checkbox",checked:!!x.nsfw,onchange:async e=>{const on=e.target.checked;
      if(on&&!confirm("Autoriser le contenu adulte (NSFW) pour « "+x.titre+" » ?\n\n• La nudité ne sera plus exclue des générations.\n• Tout personnage mineur reste exclu.\n• Uniquement des personnages fictifs ou des adultes qui ont donné leur accord : jamais la photo d'une vraie personne sans son consentement.\n• La correction par Claude ne rédige pas de contenu sexuel explicite.\n• Ces vidéos ne sont pas pour YouTube.\n\nLes vidéos déjà faites ne changent pas : marque « à refaire » celles à régénérer.")){e.target.checked=false;return}
      try{await api("/api/nsfw/"+enc(x.nom),{actif:on});toast(on?"NSFW activé pour ce projet":"NSFW désactivé : nudité exclue","ok");await charger()}catch(err){toast(err.message,"ko");e.target.checked=!on}}}),"NSFW")),
   h("div",{class:"muted petit",textContent:x.solo?x.dossier:x.dossier+"  →  rendus\\"+x.nom})),h("span",{class:"esp"}),
  x.solo||s.post?B("Dossier des vidéos",{ic:"folder",fn:()=>api("/api/ouvrir",{nom:x.nom}).catch(e=>toast(e.message,"ko"))}):null,
  x.solo?B("Supprimer le projet",{cls:"danger",ic:"trash",titre:"envoie le projet entier à la corbeille de Windows (récupérable)",fn:async()=>{
   if(!confirm("Supprimer le projet solo « "+x.titre+" » ?\n\n"+x.cases+" plan(s), vidéos, segments et essais compris.\nTout part dans la corbeille de Windows (récupérable)."))return;
   try{await api("/api/solo/"+enc(x.nom)+"/effacer",{});toast("Projet « "+x.titre+" » envoyé à la corbeille","ok");location.hash="";await charger()}
   catch(e){toast(e.message,"ko")}}}):null));
 if(!x.solo)page.append(h("div",{class:"tabs"},h("a",{class:"tab"+(onglet==="apercu"?" on":""),href:"#/p/"+E+"/apercu"},"Vue d'ensemble"),
  h("a",{class:"tab"+(onglet==="revue"?" on":""),href:"#/p/"+E+"/revue"},"Revue des cases",s["à revoir"]?h("span",{class:"badge warn"},String(s["à revoir"])):null),
  h("a",{class:"tab"+(onglet==="rendu"?" on":""),href:"#/p/"+E+"/rendu"},ic("film"),"Rendu Clip Studio")));
 page.append(onglet==="revue"?h("div",{id:"revue"},h("div",{class:"muted"},"Chargement des cases…")):onglet==="rendu"?vueRendu(x):vueApercu(x));return page}
function vueRendu(x){const N=x.nom,zone=h("div",{class:"muted"},"Chargement des rendus…"),s=x.stats;
 const aGen=s["à générer"]+s["à refaire"]+s["modifiée"];
 const page=h("div",{},h("div",{class:"card action"},h("div",{class:"txt"},h("h2",{},"Rendu du clip par Clip Studio"),
   h("div",{class:"muted"},"Clip Studio monte le clip complet (karaoké, montage sur les beats) avec les "+s.installees+" vidéo(s) installée(s)"+
    (aGen?" ; attention : "+aGen+" case(s) pas encore générée(s) ou à refaire resteront en image fixe.":".")+" Le rendu utilise surtout le processeur.")),
  h("div",{class:"rang"},B("Aperçu rapide",{ic:"play",titre:"960×540 à 15 img/s : pour vérifier vite le montage",fn:()=>lancer("rendu",{nom:N,apercu:true})}),
   B("Rendu final",{cls:"primaire grand",ic:"film",titre:"1920×1080, 30 img/s, audio : la version à publier",fn:()=>lancer("rendu",{nom:N})}))),zone);
 api("/api/rendus/"+enc(N)).then(r=>{const L=r.rendus;if(!L.length){zone.replaceChildren(h("div",{class:"card muted"},"Aucun rendu pour l'instant : lance un aperçu ou le rendu final."));return}
  const lecteur=h("video",{controls:true,playsInline:true,preload:"metadata",style:"width:100%;max-height:70vh;background:#000;border-radius:10px;display:block"});
  const titre=h("h2",{}),info=h("span",{class:"muted petit"}),tele=h("a",{class:"btn"},ic("down"),"Télécharger");
  const jouer=v=>{lecteur.src="/rendu/"+enc(N)+"/"+enc(v.nom)+"?v="+v.date;titre.textContent=v.apercu?"Aperçu rapide":v.nom;
   info.textContent=new Date(v.date*1000).toLocaleString("fr-CA",{dateStyle:"medium",timeStyle:"short"})+" · "+v.mo+" Mo";
   tele.href="/rendu/"+enc(N)+"/"+enc(v.nom)+"?telecharger=1&v="+v.date;tele.download=v.nom;
   $$(".rliste .item",zone).forEach(i=>i.classList.toggle("actif",i.dataset.nom===v.nom))};
  zone.className="";zone.replaceChildren(h("div",{class:"card"},h("div",{class:"rang",style:"margin-bottom:10px"},titre,info,h("span",{class:"esp"}),tele,
    B("Dossier",{ic:"folder",fn:()=>api("/api/ouvrir",{nom:N,quoi:"rendu"}).catch(e=>toast(e.message,"ko"))})),lecteur,
   L.length>1?h("div",{class:"rliste",style:"margin-top:12px"},L.map(v=>{const it=h("a",{class:"item",onclick:()=>jouer(v)},ic("film"),
     h("div",{},h("div",{class:"t"},v.apercu?"Aperçu rapide (960×540)":v.nom),h("div",{class:"s"},new Date(v.date*1000).toLocaleString("fr-CA",{dateStyle:"medium",timeStyle:"short"})+" · "+v.mo+" Mo")));
     it.dataset.nom=v.nom;return it})):null));jouer(L[0])}).catch(e=>zone.replaceChildren(h("div",{class:"card"},e.message)));
 return page}
function vueApercu(x){const s=x.stats,N=x.nom,E=enc(x.id),n=x.cases,aGen=s["à générer"]+s["à refaire"]+s["modifiée"];
 const etapes=[["Préparation",n+" cases"+(s.retouches?" · "+s.retouches+" prompt(s) retouché(s)":""),true],
  ["Génération",s.generees+" / "+n+" vidéos",s.generees===n&&!aGen],
  ["Revue",s.ok+" validée(s)"+(s["à revoir"]?" · "+s["à revoir"]+" à revoir":"")+(s["à refaire"]?" · "+s["à refaire"]+" à refaire":""),s.ok===n],
  ["Installation",s.installees+" / "+n+" dans Clip Studio",s.installees===n&&s.post===n&&!aGen],["Rendu final",h("a",{href:"#/p/"+E+"/rendu"},"lancer ou voir le rendu →"),false,true]];
 const ty=h("select",{},[["","toutes les cases"],["vers","couplets / refrains"],["fond","intro / instrumental / outro"]].map(([v,t])=>h("option",{value:v,textContent:t})));
 const si=h("input",{type:"checkbox"}),fi=h("input",{type:"checkbox"});
 const cpt=(k,t,c)=>h("a",{class:"compteur",href:"#/p/"+E+"/revue",onclick:()=>{S.filtre[N]=k}},h("b",{style:c?"color:var(--"+c+")":""},String(k==="gen"?s["à générer"]+s["modifiée"]:k==="risque"?s.risques:s[t])),h("span",{class:"muted petit"},k==="gen"?"à générer":k==="risque"?"prompts à risque":t));
 return h("div",{},h("div",{class:"etapes"},etapes.map(([t,d,f,ext],i)=>h("div",{class:"etape"+(f?" fait":"")+(ext?" ext":"")},
   h("div",{class:"num"},f?ic("check",{taille:14}):String(i+1)),h("div",{style:"min-width:0"},h("b",{},t),h("div",{class:"muted petit"},d))))),
  h("div",{class:"card action"},h("div",{class:"txt"},h("h2",{},aGen?aGen+" case(s) à générer":"Toutes les vidéos sont générées"),
    h("div",{class:"muted"},aGen?"Génère ce qui manque, est à refaire ou a été modifié, puis post-traite et installe dans Clip Studio · ~"+MIN(aGen)+" min.":"Met à jour le projet Clip Studio avec les dernières vidéos, puis lance le rendu dans Clip Studio.")),
   h("div",{class:"rang"},s["à revoir"]?h("a",{class:"btn grand",href:"#/p/"+E+"/revue"},ic("film"),"Revoir "+s["à revoir"]+" case(s)"):null,
    B(aGen?"Générer + installer":"Mettre à jour Clip Studio",{cls:"primaire grand",ic:"play",plein:1,fn:()=>lancer("maj",{nom:N})}))),
  h("div",{class:"compteurs"},cpt("revoir","à revoir","warn"),cpt("refaire","à refaire","ko"),cpt("gen","","info"),cpt("ok","ok","ok"),s.risques?cpt("risque","","warn"):null),
  h("div",{class:"options"},
   h("div",{class:"card"},h("h3",{},"Génération"),h("p",{class:"muted petit"},"Seulement les vidéos (sans post-traitement ni installation)."),
    h("div",{class:"rang",style:"flex-wrap:nowrap"},ty,B("Générer",{fn:()=>lancer("gen",{nom:N,type:ty.value})}))),
   h("div",{class:"card"},h("h3",{},"Corriger avec Claude"),h("p",{class:"muted petit"},"Réécrit le prompt des cases « à refaire » d'après leur note, en une seule demande légère (appel nu, ~1 500 tokens)."),
    h("div",{},B(s.annotees?"Corriger "+s.annotees+" case(s)":"Aucune case annotée",{ic:"spark",off:!s.annotees,fn:()=>lancer("corriger",{nom:N,ids:[]})}))),
   h("div",{class:"card"},h("h3",{},"Finition"),h("p",{class:"muted petit"},"16 → 30 img/s et 1920 px, puis copie dans le projet Clip Studio."),
    h("label",{class:"coche"},si,"Sans interpolation (garde 16 img/s)"),
    h("div",{class:"rang"},B("Post-traitement",{fn:()=>lancer("post",{nom:N,sans_interp:si.checked})}),B("Installer",{ic:"install",fn:()=>lancer("installer",{nom:N})}))),
   h("div",{class:"card"},h("h3",{},"Outils"),h("div",{class:"rang"},B("Vérifier les prompts",{fn:()=>lancer("verifier",{nom:N})}),
     B("Tableau des cases",{fn:()=>lancer("etat",{nom:N})}),B("Diagnostic",{ic:"pulse",fn:()=>lancer("diag",{nom:N})})),
    h("div",{class:"rang"},B("Préparer de nouveau",{ic:"refresh",titre:"relit le projet Clip Studio (nouvelles cases, descriptions changées) en gardant tes retouches",
     fn:()=>lancer("prep",{dossier:x.dossier,nom:N,fiches:fi.checked})}),h("label",{class:"coche petit"},fi,"avec fiches"))),
   h("div",{class:"card"},h("h3",{},"Cache Clip Studio"),h("p",{class:"muted petit"},
     x.cache_mo?"Images extraites des vidéos au rendu (build\\video_frames) : "+x.cache_mo+" Mo. Supprimables après le rendu final, recréées au prochain rendu.":
     "Vide : rien à libérer (il se recrée à chaque rendu Clip Studio)."),
    h("div",{},B(x.cache_mo?"Vider le cache ("+x.cache_mo+" Mo)":"Cache vide",{ic:"trash",off:!x.cache_mo,fn:async()=>{
     if(!confirm("Supprimer définitivement le cache Clip Studio de « "+x.titre+" » ("+x.cache_mo+" Mo) ?\n\nCe sont seulement les images extraites des vidéos au rendu ; Clip Studio les recrée au prochain rendu.\nNe pas le faire pendant un rendu Clip Studio."))return;
     try{const r=await api("/api/cache/"+enc(N),{});toast(r.mo+" Mo libérés","ok");charger()}catch(e){toast(e.message,"ko")}}}))),
   h("div",{class:"card"},h("h3",{},"Retirer du projet Clip Studio"),h("p",{class:"muted petit"},"Clip Studio repasse aux images fixes ; les vidéos restent dans rendus\\."),
    h("div",{class:"rang"},B("Simuler",{fn:()=>lancer("retirer",{nom:N,simulation:true})}),
     B("Retirer",{cls:"danger",ic:"trash",fn:()=>{if(confirm("Retirer du projet Clip Studio les "+s.installees+" vidéo(s) installées par Clip Motion ?"))lancer("retirer",{nom:N})}})))))}

/* ---------- revue ---------- */
async function chargerRevue(x){try{const r=await api("/api/cases/"+enc(x.nom));S.cases[x.nom]=r.cases;S.solo[x.nom]=r.solo;S.nom=x.nom;
 if(S.filtre[x.nom]==null)S.filtre[x.nom]="";rendreRevue()}catch(e){toast(e.message,"ko")}}
function liste(){const f=S.filtre[S.nom];return(S.cases[S.nom]||[]).filter(c=>{const s=etat(c);return!f||(f==="risque"?c.risques.length:f==="gen"?(s==="gen"||s==="modif"):s===f)})}
function rendreRevue(garderDetail){const zone=$("#revue");if(!zone||!S.cases[S.nom])return;const C=S.cases[S.nom],N=S.nom,solo=S.solo[N];
 const n={};C.forEach(c=>{const s=etat(c);n[s]=(n[s]||0)+1});const aGen=(n.gen||0)+(n.refaire||0)+(n.modif||0),segGen=C.filter(c=>["gen","refaire","modif"].includes(etat(c))).reduce((t,c)=>t+(c.nseg||1),0);
 const annot=C.filter(c=>c.statut==="refaire"&&c.note).length,risque=C.filter(c=>c.risques.length).length;
 const filtres=[["","Toutes",C.length],["revoir","À revoir",n.revoir||0],["refaire","À refaire",n.refaire||0],["gen","À générer",(n.gen||0)+(n.modif||0)],["ok","Validées",n.ok||0]];
 if(risque)filtres.push(["risque","⚠ Risque",risque]);
 const barre=h("div",{class:"barre"},h("div",{class:"filtres"},filtres.map(([v,t,k])=>h("button",{class:"filtre"+(S.filtre[N]===v?" on":""),type:"button",
   onclick:()=>{S.filtre[N]=v;rendreRevue()}},t,h("span",{class:"k"},String(k))))),h("span",{class:"esp"}),
  annot?B("Claude : corriger "+annot,{ic:"spark",titre:"réécrit le prompt des cases « à refaire » d'après leur note (appel nu, une seule demande)",fn:()=>lancer("corriger",{nom:N,ids:[]})}):null,
  B(aGen?"Générer "+aGen+(solo?"":" + installer")+" · ~"+MIN(segGen)+" min":solo?"Tout est généré":"Mettre à jour Clip Studio",{cls:"primaire",ic:"play",plein:1,off:solo&&!aGen,fn:()=>lancer("maj",{nom:N})}));
 const L=liste();if(S.sel[N]!=="+"&&!L.find(c=>c.id===S.sel[N]))S.sel[N]=L[0]?L[0].id:(solo?"+":null);
 const tuiles=L.map(tuile);if(solo)tuiles.unshift(h("div",{class:"tuile ajout"+(S.sel[N]==="+"?" sel":""),onclick:()=>choisir("+")},h("div",{},ic("plus",{taille:22}),h("div",{},"Ajouter un plan"))));
 const grille=h("div",{class:"grille"},tuiles.length?tuiles:h("div",{class:"muted"},"Aucune case dans ce filtre."));
 const gauche=h("div",{class:"gauche"},grille,solo?panneauAssistant():null);
 const ancien=$("#detail");S.vid=ancien?$("video",ancien):null;
 /* Fiche gardée : on remplace la barre et la mosaïque autour SANS déplacer la fiche. La déplacer pendant un clic
    (la note s'enregistre au moment où on clique sur un bouton) faisait perdre ce clic. */
 if(garderDetail&&ancien&&zone.contains(ancien)&&ancien.previousElementSibling){
  zone.firstElementChild.replaceWith(barre);ancien.previousElementSibling.replaceWith(gauche);return}
 const det=h("div",{class:"detail",id:"detail"});
 zone.replaceChildren(barre,h("div",{class:"revue"},gauche,det));rendreDetail()}
function panneauAssistant(){const N=S.nom,E=enc(N);
 const idee=h("textarea",{rows:7,value:S.idee||"",oninput:e=>{S.idee=e.target.value},
  placeholder:"Raconte ce qui doit se passer, comme à quelqu'un : « il pose sa tasse de café, se lève lentement, marche jusqu'à la fenêtre, puis se retourne et sourit à la caméra »"});
 const res=(S.aideNom===N&&S.aideRes)||h("div",{});S.aideRes=res;S.aideNom=N;
 const proposer=async e=>{const b=e.currentTarget;if(!idee.value.trim()){toast("Décris d'abord ce que tu veux voir.","ko");idee.focus();return}
  if(!S.remplirAjout){toast("Ouvre d'abord « Ajouter un plan ».","ko");return}
  const avant=b.lastChild.textContent;b.disabled=true;b.lastChild.textContent="Claude réfléchit… (~20 s)";res.replaceChildren();
  try{const r=await api("/api/solo/"+E+"/assistant",{idee:idee.value});S.remplirAjout(r);const segs=[r.prompt,...r.suites];
   res.replaceChildren(h("div",{class:"rang",style:"margin-top:14px"},h("span",{class:"badge info"},Math.round(r.duree)+" s"),
     h("span",{class:"badge"},segs.length+" segment"+(segs.length>1?"s":"")),h("span",{class:"muted petit"},"~"+Math.round(segs.length*2.9)+" min de génération")),
    r.explication?h("p",{class:"muted",style:"margin:8px 0"},r.explication):null,
    h("ol",{class:"segs"},segs.map((t,i)=>h("li",{},h("b",{},(5*i)+"–"+(5*(i+1))+" s : "),t))),
    h("p",{class:"muted petit",style:"margin:6px 0 0"},"Les champs de la fiche à droite sont remplis : relis, ajuste, puis « Ajouter et générer »."))}
  catch(err){toast(err.message,"ko")}finally{b.disabled=false;b.lastChild.textContent=avant}};
 const p=h("div",{class:"card assistant",id:"aide",hidden:S.sel[N]!=="+"},
  h("div",{class:"rang"},ic("spark",{taille:18}),h("h2",{},"Aide au prompt")),
  h("p",{class:"muted petit",style:"margin:4px 0 10px"},"Décris en tes mots ce que tu veux voir. Claude choisit la durée (une action simple par segment de 5 s) et écrit le mouvement de chaque segment, enchaînés entre eux. Il ne voit pas l'image, seulement ton texte (appel nu, ~1 500 tokens)."),
  idee,h("div",{class:"rang",style:"margin-top:10px"},B("Proposer",{cls:"primaire",ic:"spark",fn:proposer})),res);
 return p}
function tuile(c){const s=etat(c),E=enc(S.nom);
 const t=h("div",{class:"tuile"+(S.sel[S.nom]===c.id?" sel":""),title:c.id+" · "+ETATS[s].t,onclick:()=>choisir(c.id)},
  h("img",{src:"/image/"+E+"/"+c.id,loading:"lazy",alt:""}),h("span",{class:"pt "+ETATS[s].c}),h("span",{class:"lab",textContent:c.id}),
  c.risques.length?h("span",{class:"al",title:c.risques.join("\n")},"⚠"):null);t.dataset.id=c.id;
 if(c.video){t.addEventListener("mouseenter",()=>{if($("video",t))return;t.insertBefore(h("video",{src:"/video/"+E+"/"+c.id+"?v="+c.video,muted:true,loop:true,autoplay:true,playsInline:true}),t.children[1])});
  t.addEventListener("mouseleave",()=>{const v=$("video",t);if(v)v.remove()})}
 return t}
function choisir(id){S.sel[S.nom]=id;const aide=$("#aide");if(aide)aide.hidden=id!=="+";$$(".tuile").forEach(t=>t.classList.toggle("sel",(t.dataset.id||"+")===id));rendreDetail();
 if(innerWidth<1250)$("#detail").scrollIntoView({behavior:"smooth",block:"start"})}
function naviguer(dir){const L=liste(),i=L.findIndex(c=>c.id===S.sel[S.nom]),c=L[i+dir];if(!c)return;choisir(c.id);
 const t=$$(".tuile").find(t=>t.dataset.id===c.id);if(t)t.scrollIntoView({block:"nearest"})}
function rendreDetail(){const det=$("#detail");if(!det)return;const N=S.nom,E=enc(N);S.collerDans=null;S.remplirAjout=null;
 if(S.sel[N]==="+"){det.replaceChildren(formAjout());return}
 const L=liste(),c=(S.cases[N]||[]).find(x=>x.id===S.sel[N]);
 if(!c){det.replaceChildren(h("div",{class:"card muted"},"Choisis une case dans la mosaïque."));return}
 const s=etat(c),i=L.findIndex(x=>x.id===c.id),src=c.video?"/video/"+E+"/"+c.id+"?v="+c.video:null;
 let media;const vieux=S.vid;S.vid=null;
 if(src&&vieux&&vieux.dataset.src===src)media=vieux;
 else if(src){media=h("video",{src,muted:true,loop:true,autoplay:true,controls:true,playsInline:true});media.dataset.src=src}
 else media=h("img",{src:"/image/"+E+"/"+c.id,alt:"image de départ"});
 const info=h("span",{class:"muted petit"});
 const env=async ch=>{info.textContent="Enregistrement…";try{const r=await api("/api/case/"+E,{id:c.id,...ch});const C=S.cases[N];
  C[C.findIndex(x=>x.id===c.id)]=r;info.textContent="Enregistré";setTimeout(()=>{if(info.textContent==="Enregistré")info.textContent=""},1500);return r}
  catch(e){info.textContent="";toast(e.message,"ko");throw e}};
 const champ=k=>async ev=>{await env({[k]:ev.target.value});rendreRevue(true);const b=$(".dtete .badge",det),ns=etat(S.cases[N].find(x=>x.id===c.id));
  if(b){b.className="badge "+ETATS[ns].c;b.textContent=ETATS[ns].t}};
 const apres=async ch=>{const L2=liste(),k=L2.findIndex(x=>x.id===c.id),nx=(L2[k+1]||L2[k-1]||{}).id;await env(ch);
  if(nx)S.sel[N]=nx;rendreRevue();rafraichirDiscret()};
 const p=h("textarea",{value:c.prompt,rows:6,onchange:champ("prompt")});
 const no=h("input",{value:c.note,placeholder:"ex. la chemise change de couleur au milieu",onchange:champ("note")});
 const ng=h("input",{value:c.neg,placeholder:"ex. glowing mouth, extra people",onchange:champ("neg")});
 const gr=h("input",{value:c.seed,placeholder:"42",inputMode:"numeric",onchange:champ("seed")});
 det.replaceChildren(h("div",{class:"card"},
  h("div",{class:"dtete"},h("div",{style:"min-width:0"},h("h2",{textContent:c.id}),
    h("div",{class:"muted petit"},S.solo[N]?c.w+"×"+c.h+" · "+Math.round(c.duree)+" s"+(c.nseg>1?" · "+c.nseg+" segments enchaînés":""):c.type+" · "+c.duree+" s max · "+c.nb+" apparition(s)")),
   h("span",{class:"badge "+ETATS[s].c,textContent:ETATS[s].t}),c.installee?h("span",{class:"badge",title:"installée dans le projet Clip Studio"},"installée"):null,
   h("span",{class:"esp"}),B("",{cls:"icone",ic:"chevL",titre:"précédente (←)",off:i<=0,fn:()=>naviguer(-1)}),
   h("span",{class:"muted petit"},(i+1)+" / "+L.length),B("",{cls:"icone",ic:"chevR",titre:"suivante (→)",off:i>=L.length-1,fn:()=>naviguer(1)})),
  h("div",{class:"media"},media,!src?h("span",{class:"info"},"pas encore de vidéo : image de départ"):null),
  c.risques.map(r=>h("div",{class:"avert"},ic("alert"),h("span",{},r))),
  c.remede?h("div",{class:"rang",style:"margin-top:8px"},
   B(c.video?"Appliquer la correction":"Appliquer maintenant",{cls:"vert",ic:"check",titre:"ajoute aux exclusions : "+c.remede,
    fn:async()=>{await env({neg:[c.neg,c.remede].filter(x=>x&&x.trim()).join(", ")});rendreRevue()}}),
   h("span",{class:"muted petit"},c.video?"exclure « "+c.remede+" » (la case passera en « modifiée » et sera refaite)":
    "appliquée automatiquement à la génération (exclure « "+c.remede+" »)")):null,
  c.video?h("div",{class:"actions"},B("Valider",{cls:"vert",ic:"check",titre:"la vidéo est bonne (O)",fn:()=>apres({statut:"ok"})}),
   B("À refaire",{cls:"rouge",ic:"x",titre:"à régénérer : décris le défaut dans la note (R)",fn:()=>apres({statut:"refaire"})}),
   B("Autre graine",{ic:"dice",titre:"même prompt, autre tirage : souvent suffisant (G)",fn:()=>apres({seed:String(Math.floor(Math.random()*1e9)),statut:"refaire"})})):null,
  h("label",{class:"champ"},h("span",{},"Défaut constaté",h("small",{}," (note pour toi et pour Claude)")),no),
  S.solo[N]?h("label",{class:"champ"},h("span",{},"Durée",h("small",{}," (au-delà de 5 s : segments de 5 s enchaînés, ~2,9 min chacun)")),
   selectDuree(c.duree,async v=>{await env({duree:v});rendreRevue()})):null,
  h("label",{class:"champ"},h("span",{},c.nseg>1?"Mouvement · segment 1 (0–5 s)":"Mouvement",h("small",{}," (décrire le mouvement, pas l'apparence)")),p),
  c.nseg>1?champsSuites(c.suites,c.nseg,async vals=>{await env({suites:vals});rendreRevue(true)}):null,
  h("div",{class:"rang",style:"margin-top:10px"},B("Corriger avec Claude",{ic:"spark",titre:"réécrit le prompt d'après le défaut noté (appel nu, ~1 500 tokens)",
    fn:async()=>{if(!no.value.trim()){toast("Décris d'abord le défaut constaté.","ko");no.focus();return}if(no.value!==c.note)await env({note:no.value});
     if(c.statut!=="refaire")await env({statut:"refaire"});lancer("corriger",{nom:N,ids:[c.id]})}}),
   c.auto?null:B("Prompt d'origine",{ic:"undo",fn:async()=>{await env({prompt:""});rendreRevue()}}),
   c.finale?h("a",{class:"btn",href:"/finale/"+E+"/"+c.id,title:"vidéo finale 30 img/s, 1920 px"},ic("down"),"Télécharger"):null,
   S.solo[N]?B("",{cls:"icone danger",ic:"trash",titre:"supprimer ce plan (ses vidéos vont dans essais\\)",fn:async()=>{if(!confirm("Supprimer le plan « "+c.id+" » ?"))return;
    try{await api("/api/solo/"+E+"/supprimer",{id:c.id});toast("Plan supprimé","ok");await rafraichirDiscret();chargerRevue(projet(N))}catch(e){toast(e.message,"ko")}}}):null,
   h("span",{class:"esp"}),info),
  h("details",{class:"plus",open:!!c.neg},h("summary",{},"Exclusions et graine"),
   h("label",{class:"champ"},h("span",{},"Exclure en plus",h("small",{}," (anglais, séparé par des virgules)")),ng),
   h("label",{class:"champ"},h("span",{},"Graine"),gr)),
  h("p",{class:"muted petit",style:"margin:14px 0 0"},"Raccourcis : ",h("kbd",{},"O")," valider · ",h("kbd",{},"R")," à refaire · ",h("kbd",{},"G")," autre graine · ",h("kbd",{},"←")," ",h("kbd",{},"→")," naviguer")));
 if(media.play)media.play().catch(()=>{})}
const DUREES=[5,10,15,20,25,30];
const nbSeg=d=>d<=5.07?1:1+Math.ceil((d*16-81)/80);
function selectDuree(val,surChange){const v=val<=5.07?5:Math.round(val);const sel=h("select",{style:"max-width:280px"},
  DUREES.concat(DUREES.includes(v)?[]:[v]).sort((a,b)=>a-b).map(x=>h("option",{value:x,selected:x===v,
   textContent:x+" s"+(nbSeg(x)>1?" · "+nbSeg(x)+" segments · ~"+Math.round(nbSeg(x)*2.9)+" min":" · ~3 min")})));
 sel.onchange=()=>surChange(+sel.value);return sel}
function champsSuites(vals,nseg,surChange){const zone=h("div",{});const tas=[];
 for(let k=1;k<nseg;k++){const t=h("textarea",{rows:3,value:vals[k-1]||"",placeholder:"vide = même mouvement que le segment 1 ; ex. il se lève et marche vers la fenêtre"});
  if(surChange)t.onchange=()=>surChange(tas.map(x=>x.value));tas.push(t);
  zone.append(h("label",{class:"champ"},h("span",{},"Mouvement · segment "+(k+1)+" ("+(5*k)+"–"+(5*(k+1))+" s)",h("small",{}," part de la dernière image du segment précédent")),t))}
 zone.valeurs=()=>tas.map(x=>x.value);return zone}
function formAjout(){const N=S.nom,E=enc(N);let fichier=null,donnees=null,duree=5,suites=h("div",{});
 const apercu=h("div",{class:"depot",tabIndex:0},ic("image",{taille:28}),h("b",{},"Glisse une image ici ou colle-la (Ctrl+V)"),h("span",{class:"petit"},"ou clique pour la choisir · PNG, JPG, WEBP"));
 const choix=h("input",{type:"file",accept:"image/png,image/jpeg,image/webp",hidden:true});
 const lire=f=>{if(!f)return;let nom=f.name||"";
  if(!/\.(png|jpe?g|webp)$/i.test(nom)){const ext={"image/png":"png","image/jpeg":"jpg","image/webp":"webp"}[f.type];
   if(!ext){toast("Image PNG, JPG ou WEBP attendue","ko");return}nom="image-collee."+ext}
  const r=new FileReader();r.onload=()=>{fichier=nom;donnees=r.result;apercu.replaceChildren(h("img",{src:donnees,alt:""}),h("span",{class:"petit"},nom+" · cliquer pour changer"))};r.readAsDataURL(f)};
 S.collerDans=lire;
 const coller=async()=>{try{for(const it of await navigator.clipboard.read()){const t=it.types.find(x=>x.startsWith("image/"));
   if(t){lire(new File([await it.getType(t)],"image-collee."+(t==="image/jpeg"?"jpg":t.split("/")[1]),{type:t}));return}}
   toast("Pas d'image dans le presse-papiers","ko")}catch(e){toast("Accès au presse-papiers refusé : utilise Ctrl+V dans la page","ko")}};
 apercu.onclick=()=>choix.click();choix.onchange=()=>lire(choix.files[0]);
 apercu.ondragover=e=>{e.preventDefault();apercu.classList.add("survol")};apercu.ondragleave=()=>apercu.classList.remove("survol");
 apercu.ondrop=e=>{e.preventDefault();apercu.classList.remove("survol");lire(e.dataTransfer.files[0])};
 const p=h("textarea",{rows:5,placeholder:"ex. Le chat tourne lentement la tête vers la caméra et cligne des yeux ; ses moustaches frémissent. La caméra avance doucement."});
 const ng=h("input",{placeholder:"ex. extra people, text"});const fl=h("input",{type:"checkbox",checked:true});
 const lab=h("label",{class:"champ"},h("span",{},"Mouvement",h("small",{}," (gestes, déplacements, caméra ; une action simple)")),p);
 const changerDuree=(v,vals)=>{duree=v;const n=nbSeg(v),z=n>1?champsSuites(vals||(suites.valeurs?suites.valeurs():[]),n):h("div",{});
  suites.replaceWith(z);suites=z;lab.firstChild.firstChild.textContent=n>1?"Mouvement · segment 1 (0–5 s)":"Mouvement"};
 const selD=selectDuree(5,v=>changerDuree(v));
 const detNeg=h("details",{class:"plus"},h("summary",{},"Exclusions"),h("label",{class:"champ"},h("span",{},"Exclure en plus",h("small",{}," (anglais)")),ng));
 S.remplirAjout=r=>{const d=r.duree<=5.07?5:Math.round(r.duree);selD.value=String(d);changerDuree(d,r.suites);p.value=r.prompt;
  if(r.neg){ng.value=[ng.value,r.neg].filter(x=>x&&x.trim()).join(", ");detNeg.open=true}};
 const ajouter=async gen=>{if(!donnees){toast("Choisis d'abord une image.","ko");return}if(!p.value.trim()){toast("Décris le mouvement voulu.","ko");p.focus();return}
  try{const r=await api("/api/solo/"+E+"/ajouter",{fichier,image:donnees,prompt:p.value,neg:ng.value,fluide:fl.checked,
    duree,suites:suites.valeurs?suites.valeurs():[]});
   toast("Plan « "+r.id+" » ajouté","ok");S.sel[N]=r.id;await rafraichirDiscret();await chargerRevue(projet(N));if(gen)lancer("maj",{nom:N})}catch(e){toast(e.message,"ko")}};
 return h("div",{class:"card"},h("h2",{style:"margin-bottom:4px"},"Ajouter un plan"),
  h("p",{class:"muted petit",style:"margin:0 0 12px"},"L'image fixe l'apparence ; le texte décrit ce qui bouge. Format (paysage, portrait, carré) détecté tout seul, 5 à 30 s de vidéo."),
  apercu,choix,h("div",{class:"rang",style:"margin-top:8px"},B("Coller l'image",{ic:"image",titre:"image du presse-papiers (capture Win+Maj+S, image copiée…)",fn:coller}),
   h("span",{class:"muted petit"},"ou Ctrl+V n'importe où dans le projet")),
  h("label",{class:"champ"},h("span",{},"Durée",h("small",{}," (au-delà de 5 s : segments de 5 s enchaînés)")),selD),
  lab,suites,
  h("label",{class:"coche",style:"margin-top:10px"},fl,h("span",{},"Ajouter « mouvement naturel et fluide, léger mouvement de caméra, rendu cinématographique »")),
  detNeg,
  h("div",{class:"rang",style:"margin-top:16px"},B("Ajouter et générer",{cls:"primaire",ic:"play",plein:1,fn:()=>ajouter(true)}),B("Ajouter",{ic:"plus",fn:()=>ajouter(false)})))}

/* ---------- tâche ---------- */
async function majTache(){clearTimeout(majTache.t);let T;try{T=await api("/api/tache")}catch(e){majTache.t=setTimeout(majTache,5000);return}
 S.T=T;const vis=!!T.titre&&(T.en_cours||S.fermeDebut!==T.debut);$("#tiroir").hidden=!vis;document.body.classList.toggle("tache",vis);
 S.decalage=Date.now()/1000-T.maintenant;
 const faites=(T.prog||{}).faites||0,cle=T.debut+":"+faites+":"+((T.prog||{}).post||0);
 if(T.en_cours&&S.vuCle!==undefined&&cle!==S.vuCle)majPendant(T);S.vuCle=cle;
 $("#tic").replaceChildren(T.en_cours?h("span",{class:"spin"}):T.code===0?h("span",{style:"color:var(--ok)"},ic("check")):h("span",{style:"color:var(--ko)"},ic("x")));
 $("#ttitre").textContent=T.titre;$("#tstop").hidden=!T.en_cours;$("#tferme").hidden=T.en_cours;
 if(S.avant!==T.en_cours)rendreSide();dessiner();
 const pre=$("#tlog"),bas=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;pre.textContent=T.lignes.join("\n");if(bas)pre.scrollTop=pre.scrollHeight;
 if(S.avant&&!T.en_cours){const lire=["verifier","etat","diag","corriger","retirer","installer"].includes(T.args[0]);
  if(T.code!==0||lire){S.journal=true;pre.hidden=false;pre.scrollTop=pre.scrollHeight}
  toast(T.titre+(T.code===0?" : terminé":" : arrêté ou échec"),T.code===0?"ok":"ko");await charger();}
 S.avant=T.en_cours;majTache.t=setTimeout(majTache,T.en_cours?2000:6000)}
/* Avancement de la TÂCHE ENTIÈRE, en temps estimé : démarrage de ComfyUI + chaque rendu de 5 s (durée moyenne
   mesurée) + post-traitement (si maj/clip) + installation. Le % de la case en cours (fc) sert aux tuiles. */
const T_DEMARRAGE=75,T_POST=12,T_INSTALL=5;
function avancement(){const T=S.T;if(!T||!T.en_cours)return null;const p=T.prog||{},now=Date.now()/1000-(S.decalage||0);
 const moy=p.durees&&p.durees.length?p.durees.reduce((a,b)=>a+b,0)/p.durees.length:170;
 const n=p.n||0,plans=p.plans||n,avecPost=["maj","clip"].includes(T.args[0]);
 if(p.phase==="claude")return{txt:"Claude rédige les corrections…",frac:null};
 if(p.phase==="rendu_prep")return{txt:"Clip Studio prépare le rendu (analyse, plan)…",frac:null};
 if(p.phase==="rendu"){const f=p.frame||0,tot=p.total||1,ec=now-p.t;
  return{txt:"Rendu Clip Studio · image "+f+" sur "+tot,frac:Math.min(.99,f/tot),reste:f>30?ec/f*(tot-f):null}}
 if(!n){if(p.phase==="post")return{txt:"Post-traitement de "+p.cid+" ("+p.post+")",frac:null,cid:p.cid};
  if(p.phase==="installation")return{txt:"Installation dans Clip Studio",frac:null};
  return{txt:"en cours depuis "+Math.round(now-T.debut)+" s",frac:null}}
 const tDem=p.demarrage?T_DEMARRAGE:0,tPost=avecPost?plans*T_POST+T_INSTALL:0,total=tDem+n*moy+tPost;
 let fait=0,txt="…",cid=null,fc=null;
 if(p.phase==="demarrage"){fait=Math.min(tDem*.95,now-p.t);txt="Démarrage de ComfyUI"}
 else if(p.phase==="generation"){const fini=p.faites>=p.k;fc=fini?0:Math.min(.97,Math.max(0,(now-p.t)/moy));fait=tDem+(p.faites+fc)*moy;
  if(fini&&p.faites>=n)txt="Finalisation de la génération (assemblage, arrêt de ComfyUI)";
  else{cid=p.cid;txt="Génération de "+p.cid+(n>1?" · rendu "+p.k+" sur "+n:"")+" · "+Math.round(fc*100)+" % de ce rendu"}}
 else if(p.phase==="post"){fait=tDem+n*moy+Math.min(plans,Math.max(0,p.post-.5))*T_POST;cid=p.cid;
  txt="Post-traitement de "+p.cid+(plans>1?" ("+p.post+" sur "+plans+")":"")}
 else if(p.phase==="installation"){fait=total-2;txt="Installation dans Clip Studio"}
 else fait=0;
 return{txt,frac:Math.min(.99,Math.max(0,fait/total)),reste:Math.max(0,total-fait),cid,fc:cid?fc:null}}
function dessiner(){const T=S.T;if(!T||!T.titre)return;const pr=$("#tprog"),i=$("i",pr),a=avancement();
 if(a){const pct=a.frac==null?null:Math.max(0,Math.min(99,Math.round(a.frac*100)));pr.hidden=false;pr.classList.toggle("indef",pct==null);i.style.width=pct==null?"":pct+"%";
  $("#tpct").textContent=pct==null?"":pct+" %";
  $("#tinfo").textContent=a.txt+(a.reste?" · ~"+Math.max(1,Math.round(a.reste/60))+" min restantes":"");
  document.title=(pct==null?"⏳":pct+" %")+" · Clip Motion"}
 else{pr.classList.remove("indef");i.style.width=T.code===0?"100%":"0";pr.hidden=T.code!==0;$("#tpct").textContent="";
  $("#tinfo").textContent=T.code===0?"terminé en "+Math.max(1,Math.round((T.fin-T.debut)/60))+" min":"arrêté ou échec (voir le journal)";
  document.title="Clip Motion"}
 const ici=T.en_cours&&a&&a.cid&&T.args[1]===S.nom?a.cid:null;
 $$(".tuile").forEach(t=>{const on=t.dataset.id&&t.dataset.id===ici;let o=$(".enc",t);t.classList.toggle("encours",!!on);
  if(on){if(!o){o=h("div",{class:"enc"},h("span",{class:"spin"}),h("b",{}));t.append(o)}
   $("b",o).textContent=a.fc!=null?Math.round(a.fc*100)+" %":a.txt.split(" ")[0]}else if(o)o.remove()});
 const card=$("#detail .card"),ban=$("#detail .bandeau");
 if(card&&ici&&S.sel[S.nom]===ici){const txt=(a.fc!=null?"Génération en cours · "+Math.round(a.fc*100)+" %":a.txt);
  if(ban)$("span:last-child",ban).textContent=txt;else card.prepend(h("div",{class:"bandeau"},h("span",{class:"spin"}),h("span",{},txt)))}
 else if(ban)ban.remove()}
setInterval(dessiner,1000);
$("#tjour").onclick=()=>{S.journal=!S.journal;$("#tlog").hidden=!S.journal;if(S.journal)$("#tlog").scrollTop=1e9};
$("#tstop").onclick=async()=>{if(confirm("Arrêter la tâche en cours ? La case en cours de rendu est perdue, les autres sont gardées."))try{await api("/api/arreter",{})}catch(e){toast(e.message,"ko")}};
$("#tferme").onclick=()=>{S.fermeDebut=S.T&&S.T.debut;majTache()};

/* ---------- chargement ---------- */
async function rafraichirDiscret(){try{S.P=await api("/api/projets");rendreSide()}catch(e){}}
async function charger(){try{S.P=await api("/api/projets")}catch(e){$("#vue").replaceChildren(h("div",{class:"page card"},"Erreur : "+e.message));return}
 rendrePied();rendre()}
$("#bdiag").append(ic("pulse"),"Diagnostic");$("#bdiag").onclick=()=>lancer("diag",{});
$("#bmaj").append(ic("refresh"));$("#bmaj").onclick=charger;
$("#comfy").onclick=async()=>{if(!S.P||!S.P.comfyui)return;if(!confirm("Arrêter ComfyUI ? (libère la carte graphique)"))return;
 try{await api("/api/comfyui/arreter",{});toast("ComfyUI arrêté","ok");charger()}catch(e){toast(e.message,"ko")}};
$("#q").oninput=e=>{S.q=e.target.value;rendreSide()};
addEventListener("hashchange",rendre);
/* Ctrl+V d'une image : dans un projet solo, ouvre « Ajouter un plan » avec l'image collée (le texte se colle normalement) */
addEventListener("paste",e=>{const it=[...(e.clipboardData?e.clipboardData.items:[])].find(i=>i.kind==="file"&&i.type.startsWith("image/"));
 if(!it)return;const f=it.getAsFile();if(!f)return;
 if(!S.collerDans){if(!(S.nom&&S.solo[S.nom]&&$("#detail")))return;choisir("+")}
 if(S.collerDans){e.preventDefault();S.collerDans(f)}});
addEventListener("keydown",e=>{if(e.ctrlKey||e.metaKey||e.altKey||!$("#detail"))return;if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName))return;
 const k=e.key.toLowerCase(),btn=t=>$$("#detail .btn").find(b=>b.textContent.includes(t));
 if(k==="arrowright")naviguer(1);else if(k==="arrowleft")naviguer(-1);else if(k==="o"){const b=btn("Valider");if(b)b.click()}
 else if(k==="r"){const b=btn("À refaire");if(b)b.click()}else if(k==="g"){const b=btn("Autre graine");if(b)b.click()}else return;e.preventDefault()});
charger();majTache();
</script></body></html>"""
