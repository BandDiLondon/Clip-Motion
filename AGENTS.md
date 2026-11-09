# Instructions pour les assistants IA / Instructions for AI assistants (Claude Code, Codex, Cursor, Copilot…)

[Français](#français) | [English](#english) — les deux parties disent la même chose; les modifier ensemble.
Both parts say the same thing; change them together.

## Français

Clip Motion anime des images fixes en vidéos avec **Wan 2.2 image → vidéo** (GGUF) piloté par **ComfyUI portable**,
en local sous **Windows 10/11 avec une carte AMD** (ROCm). Interface web locale en français (`Clip Motion.bat` →
http://127.0.0.1:8189). Compagnon optionnel de [Clip Studio](https://github.com/BandDiLondon/Clip-Studio).

### Installer (procédure exacte)
1. **Vérifier le poste** avant tout : Windows 10/11, carte **AMD Radeon** (idéalement 16 Go de mémoire vidéo),
   ~40 Go libres, `ffmpeg` et `ffprobe` dans le `PATH` avec l'encodeur `h264_amf`
   (`ffmpeg -hide_banner -encoders | findstr h264_amf`). Sur une carte NVIDIA ou Intel : le dire à l'utilisateur
   (non testé; le post-traitement utilise `h264_amf`), ne pas improviser une adaptation sans son accord.
2. **Demander l'accord de l'utilisateur** : l'installation télécharge ~31 Go (ComfyUI 1,7 Go + modèles 29 Go).
3. Lancer, depuis le dossier du dépôt, **après son accord** :
   ```powershell
   & ".\Installer.bat" -Oui
   ```
   Le script (`installer.ps1`) installe ce qui manque : ComfyUI portable AMD **v0.44.0** dans
   `comfyui\ComfyUI_windows_portable\`, le nœud ComfyUI-GGUF (version figée) + le paquet `gguf`, et les 6 modèles.
   Chaque fichier est vérifié (taille + SHA256). Relançable : il reprend un téléchargement coupé et saute ce qui est
   déjà là. `-Verifier` contrôle aussi les empreintes des modèles présents; `-SansModeles` saute les modèles.
4. **Succès** = dernière ligne verte « Installation terminée » et code de sortie 0. En cas d'`ECHEC`, lire le message
   (il dit quoi faire), corriger la cause, relancer. Ne pas télécharger les fichiers à la main ni changer leurs noms :
   Clip Motion les appelle par leur nom exact.
5. Dire à l'utilisateur de double-cliquer sur **`Clip Motion.bat`**. Premier rendu : ~4 min pour 5 s de vidéo.

**Ne jamais** : installer dans WSL ou dans un ComfyUI existant ailleurs (le lanceur attend
`comfyui\ComfyUI_windows_portable\python_embeded\python.exe`); installer PyTorch ou d'autres paquets dans le Python
de ComfyUI (il est fourni avec PyTorch ROCm; un autre PyTorch le casse); lancer `python` sans `-s` sur ce Python.

### Aider l'utilisateur
- Il travaille dans l'interface : indique-lui le bouton plutôt qu'une commande. Ligne de commande possible depuis
  PowerShell : `& ".\Clip Motion.bat" <commande> …` (**guillemets obligatoires**, sinon cmd lance `clip.exe`);
  aide : `& ".\Clip Motion.bat" -h`.
- **Ne lance pas de génération ni de rendu Clip Studio à sa place** sans demande : une génération occupe la carte
  plusieurs minutes par plan.
- Lenteur (plus de ~3 min par tranche de 5 s) : `& ".\Clip Motion.bat" diag` ; si la mémoire « shared » de python
  est au-dessus de 0, la mémoire vidéo déborde : fermer les applications qui utilisent la carte.
- Prompts : décrire le **mouvement** (gestes, déplacements, caméra), pas l'apparence (l'image la fixe); une action
  simple par tranche de 5 s; toujours 81 images par segment.

### Modifier le code
- Python de ComfyUI uniquement, **bibliothèque standard seulement** (`outils\clipmotion.py` : commandes;
  `outils\clipmotion_web.py` : interface, page unique HTML/JS intégrée).
- **Après toute modification** :
  `comfyui\ComfyUI_windows_portable\python_embeded\python.exe -s outils\test_clipmotion.py` (10 tests, ~5 s, sans
  carte graphique). Ajouter un test pour tout bug corrigé. Tester l'interface sur un autre port (`W.PORT = 8190`).
- Le dépôt publie une **liste blanche** (`.gitignore`) : un nouveau fichier à publier doit y être ajouté. Ne jamais
  publier `comfyui\`, `rendus\` (vidéos de l'utilisateur) ni de chemin contenant son nom d'utilisateur.
- Mettre à jour `README.md` (et ce fichier) quand le fonctionnement change. Changer une version dans
  `installer.ps1` = mettre à jour sa taille et son empreinte SHA256.

## English

Clip Motion turns still images into videos with **Wan 2.2 image-to-video** (GGUF) driven by **portable ComfyUI**,
locally on **Windows 10/11 with an AMD GPU** (ROCm). Local web UI, in French (`Clip Motion.bat` →
http://127.0.0.1:8189). Optional companion of [Clip Studio](https://github.com/BandDiLondon/Clip-Studio).

### Install (exact procedure)
1. **Check the machine** first: Windows 10/11, an **AMD Radeon** GPU (ideally 16 GB VRAM), ~40 GB free, `ffmpeg` and
   `ffprobe` on `PATH` with the `h264_amf` encoder (`ffmpeg -hide_banner -encoders | findstr h264_amf`). On NVIDIA or
   Intel: tell the user (untested; post-processing uses `h264_amf`); do not improvise a port without their consent.
2. **Ask the user's consent**: the install downloads ~31 GB (ComfyUI 1.7 GB + models 29 GB).
3. Run, from the repository folder, **after consent**:
   ```powershell
   & ".\Installer.bat" -Oui
   ```
   The script (`installer.ps1`) installs what is missing: portable ComfyUI AMD **v0.44.0** into
   `comfyui\ComfyUI_windows_portable\`, the ComfyUI-GGUF node (pinned) + the `gguf` package, and the 6 models.
   Every file is checked (size + SHA256). Re-runnable: it resumes an interrupted download and skips what is already
   there. `-Verifier` also checks the hashes of existing models; `-SansModeles` skips the models.
4. **Success** = last green line "Installation terminée" and exit code 0. On `ECHEC`, read the message (it says what
   to do), fix the cause, run again. Do not download files by hand or rename them: Clip Motion uses their exact names.
5. Tell the user to double-click **`Clip Motion.bat`**. First render: ~4 min for 5 s of video.

**Never**: install in WSL or into an existing ComfyUI elsewhere (the launcher expects
`comfyui\ComfyUI_windows_portable\python_embeded\python.exe`); install PyTorch or other packages into ComfyUI's Python
(it ships with ROCm PyTorch; another PyTorch breaks it); run that Python without `-s`.

### Helping the user
- They work in the UI: point to the button rather than a command. Command line from PowerShell:
  `& ".\Clip Motion.bat" <command> …` (**quotes required**, otherwise cmd runs `clip.exe`); help:
  `& ".\Clip Motion.bat" -h`.
- **Do not start a generation or a Clip Studio render for them** unless asked: a generation keeps the GPU busy for
  several minutes per shot.
- Slow (more than ~3 min per 5 s chunk): `& ".\Clip Motion.bat" diag`; if python's "shared" memory is above 0, VRAM
  is overflowing: close the applications using the GPU.
- Prompts: describe the **motion** (gestures, movement, camera), not the look (the image sets it); one simple action
  per 5 s chunk; always 81 frames per segment.

### Changing the code
- ComfyUI's Python only, **standard library only** (`outils\clipmotion.py`: commands; `outils\clipmotion_web.py`:
  UI, single embedded HTML/JS page).
- **After any change**:
  `comfyui\ComfyUI_windows_portable\python_embeded\python.exe -s outils\test_clipmotion.py` (10 tests, ~5 s, no GPU).
  Add a test for every bug fixed. Test the UI on another port (`W.PORT = 8190`).
- The repository publishes an **allow-list** (`.gitignore`): a new file to publish must be added to it. Never publish
  `comfyui\`, `rendus\` (the user's videos) or a path containing their user name.
- Update `README.md` (and this file) when behaviour changes. Changing a version in `installer.ps1` = update its size
  and SHA256 hash.
