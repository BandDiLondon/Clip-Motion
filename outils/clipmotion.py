"""Clip Motion — anime les cases d'un projet Clip Studio, ou des images seules (projets solo), avec Wan 2.2 I2V 14B
(GGUF + LoRA lightx2v 4 étapes) via ComfyUI.

Double-clic sur « Clip Motion.bat » (sans argument) : INTERFACE WEB, tout se fait de là.
Ligne de commande (PowerShell) :  & ".\\Clip Motion.bat" <commande> …

  clip <dossier_projet_clip> [--nom N] [--fiches]   TOUT : prep → gen → post → installer (nouveau clip)
  apercu <nom>                           ouvre l'interface directement sur la revue d'un projet
  maj <nom>                              refait ce qui est à refaire / modifié → post → installer

  prep <dossier_projet_clip> [--nom N]   plans.json + images d'entrée (relançable, garde les retouches)
  ajouter <nom> <image> --prompt "…" [--duree S] [--suite "…"]…   projet SOLO : plan image + prompt
  nsfw <nom> [oui|non]                   option NSFW du projet (défaut non : nudité exclue, pour YouTube)
  effacer <nom> --oui                    projet SOLO entier → corbeille de Windows (récupérable)
  etat <nom>                             tableau des cases
  verifier <nom>                         prompts à risque (lueur au visage, tenue, nudité, texte…)
  corriger <nom> [ids…] [--simulation]   Claude (appel nu, ~1 500 tokens) réécrit les prompts d'après la note
  gen  <nom> [ids…] [--type T] [--force] génère manquantes + « à refaire » + modifiées (ComfyUI auto)
  post <nom> [ids…] [--force] [--sans-interp]   16 → 30 img/s + agrandissement (grand côté 1920 px)
  installer <nom> [--simulation]         met videos/*.mp4 dans <projet_clip>/videos/ (liens, sans doublon)
  rendu <nom> [--apercu]                 rendu Clip Studio du projet (final, ou aperçu 960×540)
  cache <nom> [--vider]                  cache Clip Studio (build/video_frames) : taille, ou suppression
  retirer   <nom> [--simulation]         retire uniquement les copies faites par installer
  diag [nom]                             vitesse récente + débordement VRAM (à lancer si c'est lent)

Données : rendus/<nom>/plans.json, par case : "prompt" (modifiable), "neg" (exclusions en plus), "seed", "frames",
"statut" (ok / refaire), "note", "fait" (réglages de la vidéo générée : sert à repérer les vidéos périmées).
Une vidéo remplacée est archivée dans essais/<case>_<date>.mp4. Bibliothèque standard seulement ;
lancé par « Clip Motion.bat » avec le Python de ComfyUI.
"""
import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
COMFY = RACINE / "comfyui" / "ComfyUI_windows_portable"
ENTREES = COMFY / "ComfyUI" / "input"
SORTIES = COMFY / "ComfyUI" / "output"
URL = "http://127.0.0.1:8188"
# Réglage mesuré le 2026-11-08 : ~2,8 min par plan de 81 images. Réserve 4 Go = débordement intermittent
# (dwm 3,6 Go + explorer/steam ~1,2 Go) → high noise 77 s/étape au lieu de 28 ; 5 Go donne de la marge.
ARGS_SERVEUR = ["--windows-standalone-build", "--disable-auto-launch", "--disable-dynamic-vram",
                "--cache-none", "--reserve-vram", "5"]
W, H, FPS_GEN, FRAMES, SEED = 832, 480, 16, 81, 42
UNET_HIGH = "Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf"
UNET_LOW = "Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf"
LORA_HIGH = "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors"
LORA_LOW = "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"
CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
VAE = "wan_2.1_vae.safetensors"
NEG_BASE = ("static, frozen, still image, slideshow, morphing face, face changing, clothes changing, outfit change, "
            "costume change, distorted hands, extra fingers, extra limbs, blurry, low quality, jpeg artifacts, watermark, "
            "text, subtitles")
# Par défaut (projet sans l'option NSFW) : nudité exclue, clips destinés à YouTube (p5_c4 : « chambre nue » → nudité).
NEG_SFW = "nudity, naked, nude, nsfw, topless, bare breasts, exposed body, lingerie, sexual content"
# Projet NSFW : la nudité n'est plus exclue, mais tout personnage mineur l'est toujours.
NEG_ADULTE = ("child, children, kid, minor, underage, teenager, teen, schoolgirl, schoolboy, young girl, young boy, "
              "childlike body, loli, shota")


def negatif(nsfw=False, neg_case=""):
    return ", ".join(x for x in (NEG_BASE, NEG_ADULTE if nsfw else NEG_SFW, neg_case) if x)
SUFFIXE = {"fr": "mouvement naturel et fluide, gestes expressifs, léger mouvement de caméra, rendu cinématographique",
           "en": "smooth natural motion, expressive gestures, subtle camera movement, cinematic"}
SUFFIXE_FOND = {"fr": "mouvement lent et fluide, aucun personnage ajouté, léger mouvement de caméra, rendu cinématographique",
                "en": "slow smooth motion, no added characters, subtle camera movement, cinematic"}
# mots ambigus que le modèle lit comme de la nudité
AMBIGUS = [(re.compile(r"\bnu(e|s|es)?\b"), lambda m: "dépouillé" + (m.group(1) or "")),
           (re.compile(r"\bnaked\b|\bbare\b", re.I), lambda m: "empty")]
CHAMPS = {"fr": ("Plan", "Action", "Décor", "Lumière"), "en": ("Shot", "Action", "Setting", "Lighting")}
TYPES_FOND = ("intro", "instrumental", "outro")

# Motifs à risque tirés des défauts réels (motif, message, mot du champ « neg » qui fait taire l'avertissement)
_LUM = r"(lueur|lumineu|brill|éclat|flamme|\bfeu\b|néon|étincel|glow|shin|fire|spark)"
_VIS = r"(bouche|visage|yeux|regard|lèvres|dents|mouth|face|eyes|teeth|lips)"
RISQUES = [
    (rf"{_LUM}[^.]*{_VIS}|{_VIS}[^.]*{_LUM}",
     "lumière près du visage : risque de lueur dans la bouche ou les yeux (p3_c2)",
     "glow"),
    (r"(enl[eè]v|retir|[ôo]te|enfil|se change|d[ée]shabill|remov|take[s]? off|put[s]? on)[^.]*"
     r"(chemise|veste|v[êe]tement|robe|habit|manteau|chapeau|shirt|jacket|cloth|dress|coat|hat)",
     "changement de tenue : le modèle le rate souvent (p2_c4)", None),
    (r"d[ée]shabill|sous-v[êe]tement|culotte|soutien-gorge|\bslip\b|torse (nu|dépouillé)|peau (nue|dépouillée)|baign|douche|\bbain\b|"
     r"underwear|shirtless|\bbath|shower|undress",  # « peau » seule (lumière sur la peau) = fausse alerte
     "mot proche de la nudité (p5_c4) : nudité déjà exclue automatiquement, vérifier quand même le rendu", None),
    (r"devien|se transform|métamorph|turns? into|\bmorph", "transformation : déformations probables", None),
    (r"\b(texte|écrit|écriteau|panneau|enseigne|lettres|inscription|sign|text|letters|lettering)\b",
     "texte à l'écran : il sera déformé", None),
    (r"\bpuis\b|\bensuite\b|\bthen\b|\bafter that\b", "plusieurs actions enchaînées en 5 s : n'en garder qu'une", None),
]
# Remèdes sûrs (exclusions ajoutées au champ « neg ») pour les risques dont la correction est connue ; appliqués
# automatiquement à la génération, ou par le bouton « Appliquer » de la revue. Clé = mot « taire » de RISQUES.
REMEDES = {"glow": "glowing mouth, light from mouth, glowing teeth, glowing eyes, light beams from face"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


# ---------- utilitaires ----------
def lire(f):
    return json.loads(Path(f).read_text(encoding="utf-8"))


_verrou = threading.Lock()


def ecrire(f, data):
    tmp = Path(f).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def maj_case(d, cid, **champs):
    """Relit plans.json, modifie une case, réécrit (la page d'aperçu et gen peuvent écrire en même temps).
    Une valeur None retire le champ."""
    with _verrou:
        p = lire(d / "plans.json")
        c = p["cases"][cid]
        for k, v in champs.items():
            if v is None:
                c.pop(k, None)
            else:
                c[k] = v
        ecrire(d / "plans.json", p)
        return c


def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:60]


def dossier(nom):
    d = RACINE / "rendus" / nom
    if not (d / "plans.json").exists():
        sys.exit(f"Pas de plans pour « {nom} » : lancer d'abord  « Clip Motion » prep <dossier_projet_clip>")
    return d


def charger(nom):
    """plans.json + amorçage : une vidéo existante sans « fait » est supposée faite avec les réglages actuels."""
    d = dossier(nom)
    p = lire(d / "plans.json")
    change = False
    for cid, c in p["cases"].items():
        if "fait" not in c and (d / "brut" / f"{cid}.mp4").exists():
            c["fait"] = reglages(c) | {"date": "amorcé"}
            change = True
    if change:
        ecrire(d / "plans.json", p)
    return d, p


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def duree_audio(f):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                           capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None


# ---------- réglages, péremption, risques ----------
def nb_segments(c):
    """Projets solo : durée > 5 s = segments de 5 s enchaînés (la dernière image d'un segment sert de départ au
    suivant ; 81 images par segment, dont la 1re reprend la précédente → +80 images = +5 s par segment)."""
    duree = float(c.get("duree") or 0)
    if duree <= FRAMES / FPS_GEN + 0.01:
        return 1
    return 1 + math.ceil((duree * FPS_GEN - FRAMES) / (FRAMES - 1))


def prompt_segment(c, k):
    suites = c.get("suites") or []
    return suites[k - 1] if k and k - 1 < len(suites) and suites[k - 1].strip() else c["prompt"]


def reglages(c, seed=None):
    r = {"prompt": c["prompt"], "neg": c.get("neg", ""), "seed": int(seed if seed is not None else c.get("seed", SEED)),
         "frames": int(c.get("frames") or c.get("frames_auto") or FRAMES)}
    if nb_segments(c) > 1:  # 5 s = comme avant (pas de champ) : les vidéos existantes ne deviennent pas périmées
        r["duree"] = float(c["duree"])
        r["suites"] = [prompt_segment(c, k) for k in range(1, nb_segments(c))]
    return r


def perimee(c):
    """La vidéo existe mais le prompt, le négatif, la graine ou le nombre d'images ont changé depuis."""
    f = c.get("fait")
    if not f:
        return False
    r = reglages(c)
    return any(f.get(k) != r[k] for k in ("prompt", "neg", "frames")) or ("seed" in c and f.get("seed") != r["seed"]) \
        or any(f.get(k) != r.get(k) for k in ("duree", "suites"))


def risques(c, personnages=(), nsfw=False):
    """Avertissements ; pour un plan en plusieurs segments, chacun est vérifié et nommé (« segment 2 (5–10 s) : … »)."""
    segs = [c["prompt"], *(c.get("suites") or [])][:nb_segments(c)] if nb_segments(c) > 1 else [c["prompt"]]
    neg = c.get("neg", "").lower()
    out = []
    for i, t in enumerate(segs):
        if not t.strip():
            continue  # segment vide = même mouvement que le premier, déjà vérifié
        for s in (*SUFFIXE.values(), *SUFFIXE_FOND.values()):
            t = t.replace(s, "")
        t = t.split("Style :")[0]
        pre = f"segment {i + 1} ({5 * i}–{5 * (i + 1)} s) : " if len(segs) > 1 else ""
        out += [pre + msg for motif, msg, taire in RISQUES
                if re.search(motif, t, re.I) and not (taire and taire in neg) and not (nsfw and "nudité" in msg)]
    if len(c["prompt"]) > 1500:
        out.append("prompt très long : la fin sera ignorée")
    return out


def remede(c, nsfw=False):
    """Exclusions à ajouter pour les risques au remède connu (texte vide si rien à faire)."""
    t = " ".join([c["prompt"], *(c.get("suites") or [])])
    for s in (*SUFFIXE.values(), *SUFFIXE_FOND.values()):
        t = t.replace(s, "")
    t = t.split("Style :")[0]
    neg = c.get("neg", "").lower()
    return ", ".join(REMEDES[taire] for motif, msg, taire in RISQUES
                     if taire in REMEDES and taire not in neg and re.search(motif, t, re.I))


def a_generer(d, cid, c, force=False):
    return force or not (d / "brut" / f"{cid}.mp4").exists() or c.get("statut") == "refaire" or perimee(c)


# ---------- prep ----------
def composer(desc, cfg, fiches=False, fond=False, nsfw=False):
    """Description Clip Studio « Plan : … ; Sujet : … ; Action : … » → prompt vidéo.
    Sans les fiches des personnages par défaut : en image → vidéo, l'image fixe déjà l'apparence ; une fiche qui
    ne correspond pas à la case fait dériver le modèle (ex. p2_c4 : vêtements qui changent en plein mouvement)."""
    langue = "en" if cfg.get("langue") == "en" else "fr"
    champs = {}
    for part in desc.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            champs[k.strip()] = v.strip().rstrip(".")
    plan, action, decor, lumiere = (champs.get(k, "") for k in CHAMPS[langue])
    if not action:
        action = desc.strip().rstrip(".")
    lieu, lum = ("Décor", "Lumière") if langue == "fr" else ("Setting", "Lighting")
    txt = ". ".join(x for x in (plan, action, decor and f"{lieu} : {decor}", lumiere and f"{lum} : {lumiere}") if x)
    for nom, fiche in (cfg.get("personnages") or {}).items() if fiches else ():
        if nom in txt:
            txt = txt.replace(nom, f"{nom} ({fiche})", 1)
    for motif, remplacement in () if nsfw else AMBIGUS:
        txt = motif.sub(remplacement, txt)
    style = cfg.get("style", "")
    fin = (SUFFIXE_FOND if fond else SUFFIXE)[langue]
    return f"{txt}. Style : {style}. {fin}." if style else f"{txt}. {fin}."


def frames_auto(c):
    """Toujours 81 (5 s). Mesuré le 2026-11-08 : moins d'images = PLUS lent (81 → 28 s/étape, 49 → 44, 33 → 71),
    car ComfyUI sous-estime alors sa mémoire de travail, charge plus de poids en VRAM et déborde en mémoire
    partagée. Clip Studio relit la vidéo depuis le début et ralentit si le plan est plus long."""
    return FRAMES


def image_case(projet, cid):
    for d in ("retouches", "cases"):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            f = projet / d / f"{cid}{ext}"
            if f.exists():
                return f
    return None


def prep(a):
    projet = Path(a.projet).resolve()
    cfg = lire(projet / "projet.json")
    descs = lire(projet / "build" / "descriptions.json") if (projet / "build" / "descriptions.json").exists() else {}
    nom = a.nom or projet.name
    d = RACINE / "rendus" / nom
    d.mkdir(parents=True, exist_ok=True)
    f = d / "plans.json"
    ancien = lire(f) if f.exists() else {}
    anciennes = ancien.get("cases", {})
    tl = [e for e in cfg.get("timeline") or [] if "t" in e]
    if not tl:
        sys.exit("Timeline vide dans projet.json : préparer le projet dans Clip Studio d'abord.")
    fin_audio = duree_audio(projet / cfg.get("audio", "")) or tl[-1]["t"] + 5
    cases = {}
    for i, e in enumerate(tl):
        cid = e.get("img")
        if not cid:
            continue
        fin = tl[i + 1]["t"] if i + 1 < len(tl) else fin_audio
        c = cases.setdefault(cid, {"type": e.get("note") if e.get("note") in TYPES_FOND else "vers",
                                   "apparitions": []})
        c["apparitions"].append(round(fin - e["t"], 2))
    prefixe = slug(nom)
    manquantes = []
    for cid, c in cases.items():
        c["duree_max"] = max(c["apparitions"])
        c["frames_auto"] = frames_auto(c)
        c["prompt_auto"] = composer(descs.get(cid, ""), cfg, a.fiches, fond=c["type"] != "vers",
                                    nsfw=bool(ancien.get("nsfw")))
        old = anciennes.get(cid, {})
        retouche = old.get("prompt") and old.get("prompt") != old.get("prompt_auto")
        c["prompt"] = old["prompt"] if retouche else c["prompt_auto"]
        for k in ("seed", "frames", "neg", "statut", "note", "fait"):
            if k in old:
                c[k] = old[k]
        src = image_case(projet, cid)
        if src is None:
            manquantes.append(cid)
            continue
        c["image"] = f"{prefixe}__{cid}.png"
        dst = ENTREES / c["image"]
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            ffmpeg("-i", src, "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}",
                   dst)
    ecrire(f, {"projet_clip": str(projet), "personnages": sorted((cfg.get("personnages") or {}).keys()),
               "installees": ancien.get("installees", []), **({"nsfw": True} if ancien.get("nsfw") else {}),
               "cases": dict(sorted(cases.items()))})
    nv = sum(c["type"] == "vers" for c in cases.values())
    print(f"{len(cases)} cases ({nv} vers, {len(cases) - nv} intro/instrumental/outro) -> {f}")
    if manquantes:
        print(f"  ATTENTION images introuvables : {', '.join(manquantes)}")
    n_risque = sum(bool(risques(c, cfg.get("personnages") or {}, bool(ancien.get("nsfw")))) for c in cases.values())
    if n_risque:
        print(f"  {n_risque} prompt(s) à risque : voir la revue dans l'interface (ou  « Clip Motion » verifier {nom})")


# ---------- projets solo (sans Clip Studio : une image + un prompt par plan) ----------
FORMATS_SOLO = {"paysage": (832, 480), "portrait": (480, 832), "carré": (624, 624)}  # ~400 000 px chacun


def creer_solo(nom, nsfw=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", nom or ""):
        raise ValueError("nom : lettres sans accent, chiffres, - et _ seulement")
    d = RACINE / "rendus" / nom
    if (d / "plans.json").exists():
        if not lire(d / "plans.json").get("solo"):
            raise ValueError(f"« {nom} » est déjà un projet Clip Studio")
        return d
    d.mkdir(parents=True, exist_ok=True)
    ecrire(d / "plans.json", {"solo": True, "projet_clip": None, "installees": [], **({"nsfw": True} if nsfw else {}),
                              "cases": {}})
    return d


def format_image(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    try:
        w, h = map(int, r.stdout.strip().split(",")[:2])
    except ValueError:
        raise ValueError("image illisible")
    return FORMATS_SOLO["portrait" if h > w * 1.15 else "paysage" if w > h * 1.15 else "carré"]


DUREE_MAX = 30


def preparer_prompt(texte, fluide=True, nsfw=False):
    texte = " ".join(str(texte).split())
    for motif, remplacement in () if nsfw else AMBIGUS:
        texte = motif.sub(remplacement, texte)
    if fluide and texte and SUFFIXE["fr"] not in texte and SUFFIXE["en"] not in texte:
        fr = re.search(r"[àâéèêëîôûç]|\b(le|la|les|et|une?|des)\b", texte, re.I)
        texte = f"{texte.rstrip('. ')}. {SUFFIXE['fr' if fr else 'en']}."
    return texte


def regler_duree(c, duree, suites=None):
    """Durée d'un plan solo (5 à 30 s) et mouvements des segments suivants (vide = même mouvement)."""
    duree = float(duree or 5)
    if not 1 <= duree <= DUREE_MAX:
        raise ValueError(f"durée entre 5 et {DUREE_MAX} s")
    if duree <= FRAMES / FPS_GEN + 0.01:
        c.pop("duree", None)
        c.pop("suites", None)
        return
    c["duree"] = duree
    n = nb_segments(c) - 1
    s = [str(x) for x in (suites if suites is not None else c.get("suites") or [])][:n]
    if any(x.strip() for x in s):
        c["suites"] = s + [""] * (n - len(s))
    else:
        c.pop("suites", None)


def ajouter_case(nom, image, prompt, neg="", fluide=True, cid=None, duree=5, suites=None):
    """Ajoute un plan à un projet solo (créé au besoin) : image recadrée au format le plus proche + prompt."""
    d = creer_solo(nom)
    nsfw = bool(lire(d / "plans.json").get("nsfw"))
    prompt = " ".join(str(prompt).split())
    if not prompt:
        raise ValueError("prompt vide : décrire le mouvement voulu")
    prompt = preparer_prompt(prompt, fluide, nsfw)
    suites = [preparer_prompt(x, fluide, nsfw) if str(x).strip() else "" for x in (suites or [])]
    w, h = format_image(image)
    base = slug(cid or Path(image).stem).lower()[:30] or "plan"
    with _verrou:
        p = lire(d / "plans.json")
        cid, k = base, 2
        while cid in p["cases"]:
            cid, k = f"{base}-{k}", k + 1
        img = f"{slug(nom)}__{cid}.png"
        try:
            ffmpeg("-i", image, "-frames:v", "1", "-vf",
                   f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}", ENTREES / img)
        except subprocess.CalledProcessError:
            raise ValueError("image illisible")
        c = {"type": "solo", "apparitions": [5.06], "duree_max": 5.06, "frames_auto": FRAMES, "prompt_auto": prompt,
             "prompt": prompt, "image": img, "w": w, "h": h}
        if neg.strip():
            c["neg"] = neg.strip()
        regler_duree(c, duree, suites)
        p["cases"][cid] = c
        ecrire(d / "plans.json", p)
    return cid


def supprimer_case(nom, cid):
    """Retire un plan d'un projet solo ; ses vidéos sont déplacées dans essais/."""
    d = dossier(nom)
    with _verrou:
        p = lire(d / "plans.json")
        if not p.get("solo") or cid not in p["cases"]:
            raise ValueError("plan inconnu (ou projet Clip Studio)")
        c = p["cases"].pop(cid)
        ecrire(d / "plans.json", p)
    date = time.strftime("%Y%m%d-%H%M%S")
    for sous in ("brut", "videos"):
        f = d / sous / f"{cid}.mp4"
        if f.exists():
            (d / "essais").mkdir(exist_ok=True)
            shutil.move(f, d / "essais" / f"{cid}_{sous}_{date}.mp4")
    (ENTREES / c.get("image", "-")).unlink(missing_ok=True)


def regler_nsfw(nom, actif):
    """Option NSFW du projet (absente = non : nudité exclue, pour YouTube). Ne rend pas les vidéos périmées :
    marquer « à refaire » celles à régénérer avec le nouveau réglage."""
    d = dossier(nom)
    with _verrou:
        p = lire(d / "plans.json")
        if actif:
            p["nsfw"] = True
        else:
            p.pop("nsfw", None)
        ecrire(d / "plans.json", p)


def nsfw(a):
    d, p = charger(a.nom)
    if a.etat:
        regler_nsfw(a.nom, a.etat == "oui")
        p = lire(d / "plans.json")
    print(f"NSFW pour « {a.nom} » : {'oui (nudité permise, mineurs toujours exclus ; pas pour YouTube)' if p.get('nsfw') else 'non (nudité exclue)'}")


def corbeille(chemins):
    """Envoie fichiers / dossiers à la corbeille de Windows (récupérables), sans boîte de dialogue."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_void_p)]

    chemins = [str(Path(c).resolve()) for c in chemins if Path(c).exists()]
    if not chemins:
        return
    liste = "\0".join(chemins) + "\0\0"  # liste terminée par deux zéros
    tampon = ctypes.create_unicode_buffer(liste, len(liste) + 1)
    # FO_DELETE=3 ; FOF_ALLOWUNDO 0x40 | FOF_NOCONFIRMATION 0x10 | FOF_SILENT 0x4 | FOF_NOERRORUI 0x400
    op = SHFILEOPSTRUCTW(None, 3, ctypes.cast(tampon, ctypes.c_void_p), None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if r or op.fAnyOperationsAborted:
        raise OSError(f"envoi à la corbeille impossible (code {r}) : un fichier est peut-être ouvert")


def effacer_solo(nom):
    """Projet solo entier (rendus/<nom> + ses images d'entrée dans ComfyUI) → corbeille. Jamais un projet Clip Studio."""
    d = dossier(nom)
    if not lire(d / "plans.json").get("solo"):
        raise ValueError(f"« {nom} » est un projet Clip Studio : effacement refusé")
    images = list(ENTREES.glob(f"{slug(nom)}__*.png"))
    corbeille([d, *images, SORTIES / "clipmotion" / slug(nom)])  # + dossier de sortie ComfyUI (vide après gen)
    return len(images)


def effacer(a):
    if not a.oui:
        sys.exit(f"Envoie le projet solo « {a.nom} » entier à la corbeille. Confirmer avec  --oui")
    try:
        n = effacer_solo(a.nom)
    except (ValueError, OSError) as e:
        sys.exit(str(e))
    print(f"Projet solo « {a.nom} » envoyé à la corbeille (dossier + {n} image(s) d'entrée).")


def ajouter(a):
    try:
        cid = ajouter_case(a.nom, a.image, a.prompt, a.neg or "", not a.brut, duree=a.duree, suites=a.suite)
    except ValueError as e:
        sys.exit(str(e))
    print(f"Plan « {cid} » ajouté à {a.nom}. Générer :  « Clip Motion » maj {a.nom}")


# ---------- état / vérifier ----------
def libelle(d, cid, c):
    if not (d / "brut" / f"{cid}.mp4").exists():
        return "à générer"
    if c.get("statut") == "refaire":
        return "à refaire"
    if perimee(c):
        return "modifiée"
    return "ok" if c.get("statut") == "ok" else "à revoir"


def etat(a):
    d, p = charger(a.nom)
    inst = set(p.get("installees", []))
    perso = p.get("personnages", [])
    print(f"{'case':8} {'type':13} {'max s':>6} {'nb':>3}  {'statut':10} video installee prompt    risques")
    compte = {}
    for cid, c in p["cases"].items():
        s = libelle(d, cid, c)
        compte[s] = compte.get(s, 0) + 1
        v = "oui  " if (d / "videos" / f"{cid}.mp4").exists() else " -   "
        i = "oui      " if f"{cid}.mp4" in inst else " -       "
        r = "retouché" if c["prompt"] != c["prompt_auto"] else "auto    "
        print(f"{cid:8} {c['type']:13} {c['duree_max']:6.2f} {len(c['apparitions']):3}  {s:10} {v} {i} {r}  "
              f"{len(risques(c, perso, p.get('nsfw'))) or '-'}")
    print("  ".join(f"{k} : {v}" for k, v in compte.items()))


def verifier(a):
    d, p = charger(a.nom)
    n = 0
    for cid, c in p["cases"].items():
        r = risques(c, p.get("personnages", []), p.get("nsfw"))
        if r:
            n += 1
            print(f"{cid} ({c['type']}) :", *r, sep="\n  - ")
    print(f"{n} case(s) à risque sur {len(p['cases'])}." if n else "Aucun prompt à risque.")


# ---------- corriger : Claude en « appel nu » (même principe que Clip Studio, clip/ai.py) ----------
# Sans outils, connecteurs MCP, réglages ni consignes de Claude Code, depuis un dossier vide : ~700 tokens de
# contexte par appel au lieu de ~34 500. Texte seul (aucune image envoyée), une seule demande pour toutes les cases.
# (--bare n'est pas utilisable : il ignore la connexion par abonnement.)
CLAUDE_OPTIONS = ["-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                  "--disable-slash-commands", "--no-session-persistence",
                  "--system-prompt", "Tu es un assistant de rédaction. Réponds exactement à la demande."]
CONSIGNE = """Tu corriges des prompts pour Wan 2.2 image→vidéo : 5 secondes, l'image de départ fixe déjà
l'apparence des personnages et du décor. Règles :
- décrire le MOUVEMENT (gestes, déplacements, caméra), pas l'apparence ; une seule action simple et lente ;
- pas de changement de vêtements, pas de transformation, pas de texte à l'écran ;
- __REGLE_NUDITE__
- plans intro / instrumental / outro : aucun personnage ajouté ;
- garder la langue du prompt et sa fin « Style : … » telle quelle ;
- corriger le défaut constaté ; « neg » = exclusions supplémentaires en anglais, séparées par des virgules
  (reprendre les exclusions actuelles utiles) ;
- si une case a « suites » : la vidéo dure plus de 5 s et enchaîne des segments de 5 s, chacun partant de la
  dernière image du précédent ; « prompt » = segment 1, « suites » = segments 2, 3… (même nombre d'éléments,
  dans l'ordre). Réécrire TOUS les segments pour une action continue et cohérente d'un segment à l'autre,
  chacun avec un mouvement simple qui prolonge le précédent (le défaut peut concerner n'importe quel segment).
Réponds UNIQUEMENT par un objet JSON : {"<case>": {"prompt": "...", "suites": ["...", ...], "neg": "..."}, ...}
(« suites » seulement pour les cases qui en ont.)

Cases :
"""


REGLE_SFW = "aucune nudité ni mot ambigu (« nu », « déshabillé »…) : clip destiné à YouTube ;"
REGLE_NSFW = "personnages adultes uniquement, jamais de mineur ni d'apparence enfantine ;"


def consigne(nsfw=False):
    return CONSIGNE.replace("__REGLE_NUDITE__", REGLE_NSFW if nsfw else REGLE_SFW)


def trouver_claude():
    """Claude Code : PATH, installation autonome, npm, puis extension VS Code la plus récente."""
    trouve = shutil.which("claude")
    if trouve:
        return trouve
    home = Path.home()
    for c in (home / ".local" / "bin" / "claude.exe", home / ".claude" / "local" / "claude",
              Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"):
        if c.is_file():
            return str(c)
    version = lambda p: tuple(int(x) for x in re.findall(r"(\d+)\.(\d+)\.(\d+)", str(p))[-1])  # noqa: E731
    bins = list((home / ".vscode" / "extensions").glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    return str(max(bins, key=version)) if bins else None


class ErreurClaude(RuntimeError):
    pass


def claude_nu(demande, timeout=600):
    """Appel nu ; lève ErreurClaude (message lisible) au lieu de quitter : utilisable aussi par l'interface web."""
    exe = trouver_claude()
    if not exe:
        raise ErreurClaude("Claude Code introuvable (extension VS Code ou installation autonome, connectée au compte).")
    try:
        with tempfile.TemporaryDirectory(prefix="clipmotion_claude_") as vide:
            r = subprocess.run([exe, *CLAUDE_OPTIONS], input=demande, cwd=vide, capture_output=True, text=True,
                               encoding="utf-8", timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ErreurClaude("Claude n'a pas répondu à temps.")
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out[out.index("{"):])
    except ValueError:
        raise ErreurClaude(f"Claude Code a échoué : {(r.stderr or out or 'réponse vide').strip()[-400:]}")
    if data.get("is_error"):
        raise ErreurClaude(f"Claude : {data.get('result') or data.get('subtype')}")
    u = data.get("usage") or {}
    entree = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    print(f"(appel nu : {entree} tokens en entrée, {u.get('output_tokens', '?')} en sortie)", flush=True)
    m = re.search(r"\{.*\}", data.get("result", ""), re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        m = None
    finally:
        if not m:  # le plus souvent un refus (ex. demande sexuellement explicite) : on montre sa réponse
            raise ErreurClaude("Claude n'a pas fait de proposition. Sa réponse :\n" + data.get("result", "").strip()[:800])


CONSIGNE_ASSISTANT = """Tu aides à écrire le prompt d'une vidéo Wan 2.2 image→vidéo. L'utilisateur fournit l'image de
départ (tu ne la vois pas) : elle fixe déjà l'apparence des personnages et du décor. Le modèle rend des segments de
5 secondes ; pour une vidéo plus longue, des segments de 5 s s'enchaînent, chacun partant de la dernière image du
précédent. À partir de ce que l'utilisateur veut voir :
- choisis la durée (5, 10, 15, 20, 25 ou 30 s) : juste assez de segments pour que chacun n'ait qu'UNE action simple et
  lente (une action par 5 s ; une idée simple = 5 s) ;
- dans un segment : un seul verbe d'action principal, jamais « puis », « ensuite », « avant de » ni une suite de
  gestes (lâche… se retourne… court…) ; une action complexe (ex. lever puis abattre) prend deux segments ;
- si l'idée compte plus d'actions que 6 segments (30 s), garde les plus importantes, simplifie, et dis-le dans
  « explication » ;
- écris le mouvement de chaque segment (gestes, déplacements, expression, caméra), pas l'apparence ; chaque segment
  prolonge le précédent (continuité de la position, du regard, de la caméra) ;
- pas de changement de vêtements, pas de transformation, pas de texte à l'écran, aucun personnage ajouté sauf demande ;
- __REGLE_NUDITE__
- écris dans la langue de l'utilisateur ; « neg » = exclusions supplémentaires en anglais, séparées par des virgules.
Réponds UNIQUEMENT par un objet JSON :
{"duree": 10, "segments": ["mouvement 0–5 s", "mouvement 5–10 s"], "neg": "...", "explication": "une phrase"}

Ce que l'utilisateur veut voir :
"""


def assistant_prompt(idee, nsfw=False):
    """Idée en langage courant → durée + mouvement de chaque segment + exclusions (appel nu). Lève ErreurClaude."""
    idee = " ".join(str(idee).split())
    if not idee:
        raise ErreurClaude("Décris d'abord ce que tu veux voir.")
    r = claude_nu(CONSIGNE_ASSISTANT.replace("__REGLE_NUDITE__", REGLE_NSFW if nsfw else REGLE_SFW) + idee[:2000])
    segs = [str(x).strip() for x in (r.get("segments") or []) if str(x).strip()][:DUREE_MAX // 5]
    if not segs:
        raise ErreurClaude("Claude n'a pas proposé de mouvement.")
    try:
        duree = float(r.get("duree") or 5 * len(segs))
    except (TypeError, ValueError):
        duree = 5 * len(segs)
    c = {"prompt": segs[0]}
    regler_duree(c, max(5, min(DUREE_MAX, duree)))
    n = nb_segments(c)
    if n != len(segs):  # durée et nombre de segments incohérents : le nombre de segments écrits fait foi
        regler_duree(c, 5 * len(segs))
        n = len(segs)
    return {"duree": float(c.get("duree") or 5), "prompt": segs[0], "suites": segs[1:n],
            "neg": str(r.get("neg") or "").strip(), "explication": str(r.get("explication") or "").strip()}


def corriger(a):
    """Réécrit le prompt (et les exclusions) des cases à partir du défaut noté dans « note ».
    Sans ids : toutes les cases « à refaire » qui ont une note. Les cases corrigées passent « à refaire »."""
    d, p = charger(a.nom)
    ids = a.ids or [cid for cid, c in p["cases"].items() if c.get("statut") == "refaire" and c.get("note")]
    lot = {}
    for cid in ids:
        if cid not in p["cases"]:
            sys.exit(f"Case inconnue : {cid}")
        c = p["cases"][cid]
        note = a.note or c.get("note")
        if not note:
            print(f"  {cid} : pas de note décrivant le défaut, ignorée")
            continue
        lot[cid] = {"type": c["type"], "prompt": c["prompt"], "neg": c.get("neg", ""), "defaut": note}
        if nb_segments(c) > 1:  # segments enchaînés : Claude les réécrit tous (vide = même mouvement → explicite)
            lot[cid]["duree_s"] = float(c["duree"])
            lot[cid]["suites"] = [prompt_segment(c, k) for k in range(1, nb_segments(c))]
    if not lot:
        print("Rien à corriger : marquer la case « À refaire » et décrire le défaut dans sa note.")
        return
    print(f"Correction par Claude de {len(lot)} case(s) : {', '.join(lot)}", flush=True)
    try:
        rep = claude_nu(consigne(p.get("nsfw")) + json.dumps(lot, ensure_ascii=False, indent=1))
    except ErreurClaude as e:
        sys.exit(str(e))
    for cid in lot:
        r = rep.get(cid)
        if not isinstance(r, dict) or not str(r.get("prompt", "")).strip():
            print(f"  {cid} : pas de réponse exploitable")
            continue
        prompt, neg = str(r["prompt"]).strip(), str(r.get("neg", "")).strip()
        print(f"\n{cid}\n  avant : {lot[cid]['prompt']}\n  après : {prompt}")
        suites = None
        if "suites" in lot[cid]:
            n = len(lot[cid]["suites"])
            rs = r.get("suites") if isinstance(r.get("suites"), list) else []
            suites = [str(x).strip() for x in rs][:n]
            suites += lot[cid]["suites"][len(suites):]  # segment oublié par Claude : on garde l'ancien
            if len(rs) < n:
                print(f"  attention : Claude n'a rendu que {len(rs)} segment(s) suivant(s) sur {n}, le reste est gardé")
            for i, (av, ap) in enumerate(zip(lot[cid]["suites"], suites), 2):
                print(f"  segment {i} avant : {av}\n  segment {i} après : {ap}")
        print(f"  exclure : {neg or '-'}")
        alertes = risques({"prompt": prompt, "suites": suites or [], "neg": neg, "type": lot[cid]["type"]}, (),
                          p.get("nsfw"))
        for x in alertes:
            print(f"  attention : {x}")
        if not a.simulation:
            champs = {"prompt": prompt, "neg": neg or None, "statut": "refaire"}
            if suites is not None:
                champs["suites"] = suites
            maj_case(d, cid, **champs)
    print("\nSimulation : rien n'a été modifié." if a.simulation else
          "\nPrompts mis à jour (cases « à refaire ») : relire dans la revue, puis Générer.")


# ---------- ComfyUI ----------
def api(chemin, data=None, timeout=10):
    req = urllib.request.Request(URL + chemin, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt.strip() else {}


def serveur_actif():
    try:
        api("/system_stats", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def demarrer_serveur():
    log = open(COMFY / "comfyui_clipmotion.log", "ab")
    p = subprocess.Popen([str(COMFY / "python_embeded" / "python.exe"), "-s", "ComfyUI/main.py", *ARGS_SERVEUR],
                         cwd=COMFY, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("Démarrage de ComfyUI (journal : comfyui_clipmotion.log)…", flush=True)
    for _ in range(180):
        if serveur_actif():
            return p
        if p.poll() is not None:
            sys.exit("ComfyUI s'est arrêté au démarrage : voir comfyui_clipmotion.log")
        time.sleep(1)
    p.terminate()
    sys.exit("ComfyUI ne répond pas après 3 min.")


def workflow(image, prompt, seed, frames, prefixe, neg_case="", w=W, h=H, nsfw=False):
    n = lambda cls, **inp: {"class_type": cls, "inputs": inp}  # noqa: E731
    return {
        "1": n("UnetLoaderGGUF", unet_name=UNET_HIGH),
        "2": n("UnetLoaderGGUF", unet_name=UNET_LOW),
        "3": n("LoraLoaderModelOnly", model=["1", 0], lora_name=LORA_HIGH, strength_model=1.0),
        "4": n("LoraLoaderModelOnly", model=["2", 0], lora_name=LORA_LOW, strength_model=1.0),
        "5": n("ModelSamplingSD3", model=["3", 0], shift=5.0),
        "6": n("ModelSamplingSD3", model=["4", 0], shift=5.0),
        "7": n("CLIPLoader", clip_name=CLIP, type="wan", device="default"),
        "8": n("VAELoader", vae_name=VAE),
        "9": n("CLIPTextEncode", text=prompt, clip=["7", 0]),
        "10": n("CLIPTextEncode", text=negatif(nsfw, neg_case), clip=["7", 0]),
        "11": n("LoadImage", image=image),
        "12": n("WanImageToVideo", positive=["9", 0], negative=["10", 0], vae=["8", 0], width=w, height=h,
                length=frames, batch_size=1, start_image=["11", 0]),
        "13": n("KSamplerAdvanced", model=["5", 0], add_noise="enable", noise_seed=seed, steps=4, cfg=1.0,
                sampler_name="euler", scheduler="simple", positive=["12", 0], negative=["12", 1],
                latent_image=["12", 2], start_at_step=0, end_at_step=2, return_with_leftover_noise="enable"),
        "14": n("KSamplerAdvanced", model=["6", 0], add_noise="disable", noise_seed=seed, steps=4, cfg=1.0,
                sampler_name="euler", scheduler="simple", positive=["12", 0], negative=["12", 1],
                latent_image=["13", 0], start_at_step=2, end_at_step=10000, return_with_leftover_noise="disable"),
        "15": n("VAEDecode", samples=["14", 0], vae=["8", 0]),
        # mp4 H.264 (CRF 18) écrit directement par ComfyUI : plus rapide que 81 PNG + ffmpeg
        "16": n("CreateVideo", images=["15", 0], fps=float(FPS_GEN), codec="h264"),
        "17": n("SaveVideo", video=["16", 0], filename_prefix=f"{prefixe}/v", format="auto", codec="auto"),
    }


def attendre(pid, cid, t0):
    dernier = 0
    while True:
        h = api(f"/history/{pid}").get(pid)
        if h and h.get("status", {}).get("completed") is not None:
            st = h["status"]
            if st.get("status_str") != "success":
                msg = next((m[1].get("exception_message", "") for m in st.get("messages", [])
                            if m[0] == "execution_error"), "")
                raise RuntimeError(f"échec ComfyUI : {msg.strip()[:300]}")
            return h
        if time.time() - dernier > 60:
            print(f"  {cid} : {time.time() - t0:.0f} s…", flush=True)
            dernier = time.time()
        time.sleep(3)


def nb_frames(c, a):
    """Priorité : "frames" écrit à la main dans plans.json > --frames > calcul automatique."""
    return int(c.get("frames") or a.frames or c.get("frames_auto") or FRAMES)


def video_grise(f):
    """Vrai si la plupart des images sont unies (aucun contraste) : rendu raté, typiquement une image grise."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-vf", "fps=2,signalstats,metadata=print:file=-",
                        "-f", "null", "-"], capture_output=True, text=True)
    mins = [float(x) for x in re.findall(r"YMIN=([\d.]+)", r.stdout)]
    maxs = [float(x) for x in re.findall(r"YMAX=([\d.]+)", r.stdout)]
    plages = [b - a for a, b in zip(mins, maxs)]
    return bool(plages) and sum(p < 8 for p in plages) > len(plages) / 2


def assembler(segments, sortie, duree):
    """Colle les segments (la 1re image des suivants = dernière du précédent, retirée) et coupe à la durée voulue."""
    entrees = [x for f in segments for x in ("-i", f)]
    n = len(segments)
    filtre = "".join(f"[{i}:v]" + ("trim=start_frame=1,setpts=PTS-STARTPTS" if i else "null") + f"[v{i}];" for i in range(n))
    filtre += "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    ffmpeg(*entrees, "-filter_complex", filtre, "-map", "[v]", "-t", f"{duree:.3f}", "-c:v", "libx264", "-crf", "18",
           "-pix_fmt", "yuv420p", sortie)


def gen(a):
    """Génère les cases sans vidéo, marquées « refaire » ou modifiées depuis leur vidéo (--force : toutes celles
    demandées). L'ancienne vidéo est archivée dans essais/ ; la nouvelle repasse « à revoir ».
    Plans solo de plus de 5 s : segments de 5 s enchaînés puis assemblés (rendus/<nom>/segments/<case>/)."""
    d, p = charger(a.nom)
    ids = selection(p, a)
    (d / "brut").mkdir(exist_ok=True)
    a_faire = [c for c in ids if a_generer(d, c, p["cases"][c], a.force)]
    if not a_faire:
        print("Rien à générer (--force pour refaire).")
        return
    perso = p.get("personnages", [])
    for cid in a_faire:
        for r in risques(p["cases"][cid], perso, p.get("nsfw")):
            print(f"  attention {cid} : {r}")
    njobs = sum(nb_segments(p["cases"][c]) for c in a_faire)
    # ~2,9 min par rendu de 81 images une fois ComfyUI échauffé (+ ~1,5 min au premier)
    mins = sum(2.9 * nb_frames(p["cases"][c], a) / FRAMES * nb_segments(p["cases"][c]) for c in a_faire) + 1.5
    print(f"{len(a_faire)} plan(s) à générer ({', '.join(a_faire)}) = {njobs} rendu(s) de 5 s, ~{mins:.0f} min estimées.",
          flush=True)
    serveur = None if serveur_actif() else demarrer_serveur()
    journal = open(d / "journal.txt", "a", encoding="utf-8")
    debut = t_prec = time.time()
    file, actifs, en_cours, k = list(a_faire), [], {}, 0

    def envoyer(cid, st):
        c, sg = st["c"], st["seg"]
        image = c["image"] if sg == 0 else st["img_suite"]
        pref = f"clipmotion/{slug(a.nom)}/{cid}_{sg}_{int(time.time())}"
        pid = api("/prompt", {"prompt": workflow(image, prompt_segment(c, sg), st["seed"] + sg, st["frames"], pref,
                                                   c.get("neg", ""), c.get("w", W), c.get("h", H),
                                                   bool(lire(d / "plans.json").get("nsfw"))),
                              "client_id": "clipmotion"})["prompt_id"]
        actifs.append((cid, sg, pid, pref))

    def soumettre():
        """Garde 2 rendus dans la file de ComfyUI. Le segment suivant d'un plan passe en priorité dès que le
        précédent est fini. plans.json est relu au démarrage de chaque plan (retouche en cours de lot prise)."""
        while len(actifs) < 2:
            pret = next((cid for cid, st in en_cours.items() if st.get("pret")), None)
            if pret:
                en_cours[pret]["pret"] = False
                envoyer(pret, en_cours[pret])
                continue
            if not file:
                break
            cid = file.pop(0)
            c = lire(d / "plans.json")["cases"][cid]
            if not a_generer(d, cid, c, a.force):
                continue
            if "image" not in c:
                print(f"  {cid} : pas d'image, ignorée")
                continue
            ajout = remede(c, bool(lire(d / "plans.json").get("nsfw")))
            if ajout:  # risque au remède connu : exclusions ajoutées avant le rendu
                c["neg"] = ", ".join(x for x in (c.get("neg", "").strip(), ajout) if x)
                maj_case(d, cid, neg=c["neg"])
                print(f"  {cid} : correction automatique, exclure « {ajout} »", flush=True)
            frames, seed, nseg = nb_frames(c, a), int(c.get("seed", a.seed)), nb_segments(c)
            dos = d / "segments" / cid
            shutil.rmtree(dos, ignore_errors=True)
            if nseg > 1:
                dos.mkdir(parents=True)
            en_cours[cid] = {"c": c, "seg": 0, "nseg": nseg, "seed": seed, "frames": frames, "dos": dos,
                             "regl": reglages(c, seed) | {"frames": frames}}
            envoyer(cid, en_cours[cid])

    def terminer(cid, st, fichier):
        cible = d / "brut" / f"{cid}.mp4"
        if cible.exists():  # archive : on ne perd jamais une version
            (d / "essais").mkdir(exist_ok=True)
            date = time.strftime("%Y%m%d-%H%M%S", time.localtime(cible.stat().st_mtime))
            shutil.move(cible, d / "essais" / f"{cid}_{date}.mp4")
        shutil.move(fichier, cible)
        maj_case(d, cid, fait=st["regl"] | {"date": time.strftime("%F %T")}, statut=None, seed=st["seed"])

    try:
        soumettre()
        while actifs:
            cid, sg, pid, pref = actifs[0]
            st = en_cours[cid]
            k += 1
            seg_txt = f", segment {sg + 1}/{st['nseg']}" if st["nseg"] > 1 else ""
            print(f"[{k}/{njobs}] {cid} ({st['c']['type']}{seg_txt}, {st['frames']} img, seed {st['seed'] + sg})",
                  flush=True)
            try:
                h = attendre(pid, cid, t_prec)
            except RuntimeError as e:
                actifs.pop(0)
                en_cours.pop(cid, None)
                soumettre()
                print(f"  ECHEC {cid} : {e}", flush=True)
                journal.write(f"{time.strftime('%F %T')} {cid} ECHEC {e}\n")
                continue
            actifs.pop(0)
            sortie = next(o for o in h["outputs"].values() if o.get("images"))["images"][0]
            fichier = SORTIES / sortie["subfolder"] / sortie["filename"]
            if video_grise(fichier):
                # Carte graphique « décrochée » (vu le 2026-11-08 sous ROCm Windows : étapes 20× trop rapides, image
                # grise unie, aucune erreur). On ne garde pas ce rendu ; ComfyUI est redémarré et le rendu refait.
                Path(fichier).unlink(missing_ok=True)
                shutil.rmtree(SORTIES / pref, ignore_errors=True)
                journal.write(f"{time.strftime('%F %T')} {cid} rendu gris (segment {sg + 1})\n")
                if serveur is None or st.get("relance"):
                    raison = "ComfyUI lancé à part : le redémarrer" if serveur is None else "encore gris après redémarrage"
                    print(f"  ECHEC {cid} : rendu gris ({raison})", flush=True)
                    journal.write(f"{time.strftime('%F %T')} {cid} ECHEC rendu gris\n")
                    en_cours.pop(cid, None)
                    soumettre()
                    continue
                print(f"  {cid} : rendu gris (la carte graphique a décroché) → redémarrage de ComfyUI et nouvel essai",
                      flush=True)
                st["relance"] = True
                for autre, sg2, _, _ in actifs:  # la file de ComfyUI sera perdue : tout ce qui y était est renvoyé
                    en_cours[autre].update(seg=sg2, pret=True)
                actifs.clear()
                st.update(seg=sg, pret=True)
                try:
                    api("/queue", {"clear": True})
                except (urllib.error.URLError, OSError):
                    pass
                serveur.terminate()
                try:
                    serveur.wait(30)
                except subprocess.TimeoutExpired:
                    serveur.kill()
                serveur = demarrer_serveur()
                k -= 1
                t_prec = time.time()
                soumettre()
                continue
            st.pop("relance", None)
            dt, t_prec = time.time() - t_prec, time.time()
            if st["nseg"] > 1:
                seg_f = st["dos"] / f"s{sg}.mp4"
                shutil.move(fichier, seg_f)
                shutil.rmtree(SORTIES / pref, ignore_errors=True)
                if sg + 1 < st["nseg"]:  # dernière image → départ du segment suivant
                    img = f"{slug(a.nom)}__{cid}__s{sg + 1}.png"
                    ffmpeg("-sseof", "-0.5", "-i", seg_f, "-update", "1", ENTREES / img)
                    st.update(seg=sg + 1, img_suite=img, pret=True)
                    soumettre()
                    print(f"  OK {cid} segment {sg + 1}/{st['nseg']} en {dt / 60:.1f} min", flush=True)
                    continue
                soumettre()
                tmp = st["dos"] / "assemble.mp4"
                assembler([st["dos"] / f"s{i}.mp4" for i in range(st["nseg"])], tmp, float(st["c"]["duree"]))
                terminer(cid, st, tmp)
                for i in range(1, st["nseg"]):
                    (ENTREES / f"{slug(a.nom)}__{cid}__s{i}.png").unlink(missing_ok=True)
                shutil.rmtree(st["dos"], ignore_errors=True)
            else:
                soumettre()
                terminer(cid, st, fichier)
                shutil.rmtree(SORTIES / pref, ignore_errors=True)
            en_cours.pop(cid, None)
            print(f"  OK {cid} en {dt / 60:.1f} min", flush=True)
            journal.write(f"{time.strftime('%F %T')} {cid} OK {dt:.0f}s frames={st['frames']} seed={st['seed']}"
                          f"{' segments=' + str(st['nseg']) if st['nseg'] > 1 else ''}\n")
            journal.flush()
    except KeyboardInterrupt:
        print("\nInterrompu : arrêt du rendu en cours.")
        try:
            api("/queue", {"clear": True})
            api("/interrupt", {})
        except (urllib.error.URLError, OSError):
            pass
        raise SystemExit(1)
    finally:
        journal.close()
        if serveur:
            serveur.terminate()
            print("ComfyUI arrêté.")
        print(f"Terminé en {(time.time() - debut) / 60:.0f} min.")


# ---------- post ----------
def post(a):
    d, p = charger(a.nom)
    (d / "videos").mkdir(exist_ok=True)
    echelle = "scale='if(gte(iw,ih),1920,-2)':'if(gte(iw,ih),-2,1920)':flags=lanczos"  # grand côté 1920 px
    vf = echelle if a.sans_interp else "minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1," + echelle
    for cid in selection(p, a):
        src, dst = d / "brut" / f"{cid}.mp4", d / "videos" / f"{cid}.mp4"
        if not src.exists() or (dst.exists() and not a.force and dst.stat().st_mtime > src.stat().st_mtime):
            continue
        t0 = time.time()
        ffmpeg("-i", src, "-vf", vf, "-c:v", "h264_amf", "-quality", "quality", "-b:v", "12M", "-pix_fmt", "yuv420p",
               "-an", dst)
        print(f"{cid} -> videos/{cid}.mp4 ({time.time() - t0:.0f} s)")


# ---------- installer / retirer ----------
def installer(a):
    d, p = charger(a.nom)
    sim = getattr(a, "simulation", False)
    if not p.get("projet_clip"):
        print(f"Projet solo : rien à installer (vidéos dans rendus/{a.nom}/videos).")
        return
    cible = Path(p["projet_clip"]) / "videos"
    if not sim:
        cible.mkdir(exist_ok=True)
    inst = set(p.get("installees", []))
    liees = copiees = 0
    for v in sorted((d / "videos").glob("*.mp4")):
        dst = cible / v.name
        if dst.exists() and v.name not in inst:
            print(f"  {v.name} : déjà présent dans le projet et pas installé par Clip Motion, laissé tel quel")
            continue
        if dst.exists() and os.path.samefile(v, dst):  # déjà lié : même fichier, rien à faire
            inst.add(v.name)
            continue
        if sim:
            print(f"  {'remplacerait la copie par un lien' if dst.exists() else 'lierait'} {v.name}")
            liees += 1
            continue
        # Lien physique : pour Clip Studio c'est un fichier normal, mais sans doublon sur le disque. Créé à côté puis
        # mis à la place d'un coup ; si le lien est impossible (autre disque…), copie classique.
        tmp = dst.with_name(dst.name + ".lien")
        tmp.unlink(missing_ok=True)
        try:
            os.link(v, tmp)
            os.replace(tmp, dst)
            liees += 1
        except OSError:
            tmp.unlink(missing_ok=True)
            shutil.copy2(v, dst)
            copiees += 1
        inst.add(v.name)
    if sim:
        print(f"Simulation : {liees} vidéo(s) à installer dans {cible}, rien n'a été modifié.")
        return
    p = lire(d / "plans.json")  # relu : gen ou la page ont pu écrire entre-temps
    p["installees"] = sorted(inst)
    ecrire(d / "plans.json", p)
    print(f"{liees} liée(s) sans doublon, {copiees} copiée(s) ; {len(inst)} vidéo(s) dans {cible} "
          f"(Clip Studio les utilise au prochain rendu).")


def clip_studio(nom):
    """(racine de Clip Studio, dossier du projet) pour un projet Clip Studio, ou une erreur explicite."""
    p = lire(dossier(nom) / "plans.json")
    if not p.get("projet_clip"):
        raise ValueError("projet solo : pas de rendu Clip Studio")
    projet = Path(p["projet_clip"])
    racine = projet.parents[1]  # <racine>/projets/<projet>
    if not (racine / ".venv" / "Scripts" / "python.exe").is_file() or not (racine / "clip").is_dir():
        raise ValueError(f"Clip Studio introuvable dans {racine} (lancer Installer.bat de Clip Studio)")
    return racine, projet


def rendus_clip(nom):
    """Vidéos produites par Clip Studio (build/*.mp4), les plus récentes d'abord."""
    _, projet = clip_studio(nom)
    return sorted((projet / "build").glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True) \
        if (projet / "build").is_dir() else []


def rendu(a):
    """Lance le rendu de Clip Studio (sa propre commande, comme son bouton Rendu) pour ce projet."""
    try:
        racine, projet = clip_studio(a.nom)
    except ValueError as e:
        sys.exit(str(e))
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", CLIP_LANG="fr")
    ff = racine / "tools" / "ffmpeg" / "bin"
    if ff.is_dir():
        env["PATH"] = str(ff) + os.pathsep + env.get("PATH", "")
    print(f"Rendu Clip Studio ({'aperçu 960×540' if a.apercu else 'final'}) de « {projet.name} »…", flush=True)
    r = subprocess.run([str(racine / ".venv" / "Scripts" / "python.exe"), "-m", "clip",
                        "preview" if a.apercu else "render", projet.name], cwd=racine, env=env)
    if r.returncode:
        sys.exit(f"Le rendu Clip Studio a échoué (code {r.returncode}).")
    print("Rendu Clip Studio terminé.")


def taille_dossier(dos):
    return sum(f.stat().st_size for f in Path(dos).rglob("*") if f.is_file()) if Path(dos).is_dir() else 0


def vider_cache_clip(nom):
    """Supprime <projet_clip>/build/video_frames (images extraites par Clip Studio au rendu ; recréé au prochain)."""
    d = dossier(nom)
    projet = lire(d / "plans.json").get("projet_clip")
    if not projet:
        raise ValueError("projet solo : pas de cache Clip Studio")
    cache = Path(projet) / "build" / "video_frames"
    if not cache.is_dir():
        return 0
    taille = taille_dossier(cache)
    shutil.rmtree(cache)
    return taille


def cache(a):
    d, p = charger(a.nom)
    projet = p.get("projet_clip")
    if not projet:
        sys.exit("Projet solo : pas de cache Clip Studio.")
    c = Path(projet) / "build" / "video_frames"
    if not a.vider:
        print(f"Cache Clip Studio : {taille_dossier(c) / 1e6:.0f} Mo ({c}). Le supprimer :  cache {a.nom} --vider")
        return
    try:
        n = vider_cache_clip(a.nom)
    except (ValueError, OSError) as e:
        sys.exit(f"Impossible : {e} (rendu Clip Studio en cours ?)")
    print(f"Cache Clip Studio supprimé : {n / 1e6:.0f} Mo libérés (recréé au prochain rendu).")


def retirer(a):
    d, p = charger(a.nom)
    if not p.get("projet_clip"):
        print("Projet solo : rien à retirer.")
        return
    cible = Path(p["projet_clip"]) / "videos"
    noms = p.get("installees", [])
    presents = [n for n in noms if (cible / n).exists()]
    if getattr(a, "simulation", False):
        print(f"Simulation : retirerait {len(presents)} vidéo(s) de {cible} :", " ".join(presents) or "-")
        autres = sorted(f.name for f in cible.glob("*") if f.name not in noms) if cible.exists() else []
        print(f"Laisserait {len(autres)} fichier(s) non installé(s) par Clip Motion :", " ".join(autres) or "-")
        return
    for name in presents:
        (cible / name).unlink(missing_ok=True)
    print(f"{len(presents)} vidéo(s) retirée(s) de {cible}.")
    p["installees"] = []
    ecrire(d / "plans.json", p)


# ---------- diag ----------
PS_VRAM = ("Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage','\\GPU Process Memory(*)\\Shared Usage' "
           "-ErrorAction SilentlyContinue | % { $_.CounterSamples } | ? { $_.CookedValue -gt 200MB } | % { "
           "$n = if ($_.InstanceName -match 'pid_(\\d+)') { (Get-Process -Id $matches[1] -ErrorAction SilentlyContinue)"
           ".ProcessName } else { '?' }; '{0,-16} {1,-16} {2:N2} Go' -f $n, ($_.Path -split '\\\\')[-1], "
           "($_.CookedValue/1GB) }")


def diag(a):
    """Vitesse récente (journal ComfyUI + journal du projet) et débordement VRAM : remplace les vérifs à la main."""
    log = COMFY / "comfyui_clipmotion.log"
    if log.exists():
        txt = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text(encoding="utf-8", errors="replace")).replace("\r", "\n")
        etapes = re.findall(r"2/2 \[\d\d:\d\d<00:00, *([\d.]+)s/it", txt)[::2]  # chaque barre est écrite 2 fois
        execs = re.findall(r"Prompt executed in ([\d.:]+)", txt)
        print(f"s/étape (dernières) : {', '.join(etapes[-8:]) or '-'}   (normal ~28 ; >40 = débordement VRAM)")
        print(f"durée par case (s)  : {', '.join(execs[-5:]) or '-'}   (normal ~165 pour 81 images)")
    else:
        print("Pas de journal ComfyUI (comfyui_clipmotion.log) : gen pas encore lancé par Clip Motion.")
    if a.nom and (RACINE / "rendus" / a.nom / "journal.txt").exists():
        print("journal :", *(RACINE / "rendus" / a.nom / "journal.txt").read_text(encoding="utf-8").splitlines()[-3:],
              sep="\n  ")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_VRAM], capture_output=True, text=True)
    print("VRAM par processus (python « shared usage » doit rester ~0) :")
    print(r.stdout.rstrip() or "  (aucun compteur)")
    print(f"ComfyUI actif : {'oui' if serveur_actif() else 'non'}")


# ---------- enchaînements ----------
def maj(a):
    """Après une revue : régénère ce qui est à refaire / modifié / manquant → post → installer."""
    base = dict(nom=a.nom, ids=[], type=None, force=False)
    gen(argparse.Namespace(**base, seed=SEED, frames=None))
    post(argparse.Namespace(**base, sans_interp=False))
    if lire(dossier(a.nom) / "plans.json").get("projet_clip"):
        installer(argparse.Namespace(nom=a.nom, simulation=False))
        print("Prêt. Revoir les nouvelles vidéos dans l'interface (Clip Motion → Revue), puis rendu dans Clip Studio.")
    else:
        print(f"Prêt. Vidéos finales dans rendus\\{a.nom}\\videos (à revoir dans l'interface).")


def clip(a):
    """prep → gen → post → installer. Le rendu Clip Studio reste à lancer par l'auteur (AGENTS.md de Clip Studio)."""
    nom = a.nom or Path(a.projet).resolve().name
    prep(argparse.Namespace(projet=a.projet, nom=nom, fiches=getattr(a, "fiches", False)))
    maj(argparse.Namespace(nom=nom))


def selection(p, a):
    ids = list(p["cases"])
    if getattr(a, "ids", None):
        inconnus = [i for i in a.ids if i not in p["cases"]]
        if inconnus:
            sys.exit(f"Cases inconnues : {', '.join(inconnus)}")
        ids = a.ids
    if getattr(a, "type", None):
        ids = [i for i in ids if (p["cases"][i]["type"] == "vers") == (a.type == "vers")]
    return ids


# ---------- interface web ----------
def ui(a):
    """Interface web (outils/clipmotion_web.py) : projets, options, tâches en direct, revue des cases."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I retire le dossier du script de sys.path
    import clipmotion_web
    clipmotion_web.ui(sys.modules[__name__], revue=getattr(a, "nom", None), navigateur=not a.sans_navigateur)


def _interrompre(*_):
    raise KeyboardInterrupt  # Ctrl+Pause envoyé par l'interface web = Ctrl+C


def main():
    ap = argparse.ArgumentParser(prog="Clip Motion", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("prep"); s.add_argument("projet"); s.add_argument("--nom")
    s.add_argument("--fiches", action="store_true", help="ajoute la fiche des personnages dans chaque prompt")
    s = sp.add_parser("etat"); s.add_argument("nom")
    s = sp.add_parser("verifier"); s.add_argument("nom")
    s = sp.add_parser("corriger"); s.add_argument("nom"); s.add_argument("ids", nargs="*")
    s.add_argument("--note", help="défaut constaté (sinon : champ note de chaque case)")
    s.add_argument("--simulation", action="store_true", help="affiche la proposition sans modifier plans.json")
    s = sp.add_parser("ajouter", help="projet solo : ajoute un plan (image + prompt)")
    s.add_argument("nom"); s.add_argument("image"); s.add_argument("--prompt", required=True)
    s.add_argument("--neg"); s.add_argument("--brut", action="store_true", help="sans les mots de fluidité ajoutés")
    s.add_argument("--duree", type=float, default=5, help="5 à 30 s (au-delà de 5 s : segments de 5 s enchaînés)")
    s.add_argument("--suite", action="append", help="mouvement du segment suivant (répétable ; absent = même mouvement)")
    s = sp.add_parser("rendu", help="lance le rendu Clip Studio du projet (--apercu : 960×540 rapide)")
    s.add_argument("nom"); s.add_argument("--apercu", action="store_true")
    s = sp.add_parser("cache", help="cache Clip Studio (build/video_frames) : taille, ou --vider")
    s.add_argument("nom"); s.add_argument("--vider", action="store_true")
    s = sp.add_parser("nsfw", help="affiche ou règle l'option NSFW du projet")
    s.add_argument("nom"); s.add_argument("etat", nargs="?", choices=["oui", "non"])
    s = sp.add_parser("effacer", help="projet solo : envoie le projet entier à la corbeille")
    s.add_argument("nom"); s.add_argument("--oui", action="store_true", help="confirme l'effacement")
    s = sp.add_parser("gen"); s.add_argument("nom"); s.add_argument("ids", nargs="*")
    s.add_argument("--type", choices=["vers", "fond"]); s.add_argument("--force", action="store_true")
    s.add_argument("--seed", type=int, default=SEED); s.add_argument("--frames", type=int)
    s = sp.add_parser("post"); s.add_argument("nom"); s.add_argument("ids", nargs="*")
    s.add_argument("--type", choices=["vers", "fond"]); s.add_argument("--force", action="store_true")
    s.add_argument("--sans-interp", action="store_true")
    s = sp.add_parser("installer"); s.add_argument("nom"); s.add_argument("--simulation", action="store_true")
    s = sp.add_parser("retirer"); s.add_argument("nom"); s.add_argument("--simulation", action="store_true")
    s = sp.add_parser("diag"); s.add_argument("nom", nargs="?")
    s = sp.add_parser("ui"); s.add_argument("--sans-navigateur", action="store_true")
    s = sp.add_parser("apercu"); s.add_argument("nom"); s.add_argument("--sans-navigateur", action="store_true")
    s = sp.add_parser("maj"); s.add_argument("nom")
    s = sp.add_parser("clip"); s.add_argument("projet"); s.add_argument("--nom")
    s.add_argument("--fiches", action="store_true")
    a = ap.parse_args(sys.argv[1:] or ["ui"])  # sans argument (double-clic sur « Clip Motion.bat ») : interface web
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _interrompre)
    if (getattr(a, "frames", None) or 81) % 4 != 1:
        sys.exit("--frames doit valoir 4n+1 (ex. 49, 65, 81).")
    try:
        {"prep": prep, "ajouter": ajouter, "effacer": effacer, "nsfw": nsfw, "cache": cache, "rendu": rendu, "etat": etat, "verifier": verifier, "corriger": corriger, "gen": gen, "post": post, "installer": installer,
         "retirer": retirer, "diag": diag, "ui": ui, "apercu": ui, "maj": maj, "clip": clip}[a.cmd](a)
    except KeyboardInterrupt:
        sys.exit("Interrompu.")


if __name__ == "__main__":
    main()
