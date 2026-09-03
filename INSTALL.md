# Installation depuis les sources

Ce document s'adresse à qui veut faire tourner depharass depuis le code Python,
ou reconstruire l'exécutable distribué. Pour simplement utiliser l'application,
voir [README.md](README.md) — aucune installation n'y est nécessaire.

## Prérequis

- **Python 3.9+** et `pip`.
- Un moteur de rendu web pour la fenêtre :
  - **Windows** : aucune installation — le runtime **WebView2** est déjà présent sur un Windows 10/11 à jour (livré avec Edge). S'il manque : [Evergreen Standalone Installer](https://developer.microsoft.com/microsoft-edge/webview2/).
  - **Linux** : bindings **GTK** ou **Qt**, à installer explicitement (voir ci-dessous) — rien n'est présent par défaut dans un environnement Python isolé.
  - **macOS** : aucune installation — WKWebView est natif au système.

## Installation et lancement — Linux / macOS

Mêmes commandes de `venv`/`pip`/lancement sur les deux systèmes ; seul le prérequis du moteur de rendu diffère, et il se règle **avant** `pip install` ci-dessous — jamais la même commande sur les deux, `apt` n'existe pas sur macOS :

- **Linux** — rien n'est présent par défaut (cf. Prérequis), installer un backend GTK ou Qt :

  ```bash
  sudo apt install python3-gi gir1.2-webkit2-4.1   # backend GTK, le plus leger
  # si indisponible/echoue au lancement, Qt en remplacement :
  #   pip install qtpy pyqt6 PyQt6-WebEngine
  ```

- **macOS** — rien à installer ici, WKWebView est natif au système (cf. Prérequis). Deux choses à savoir avant de lancer `pip install` :
  - Python fourni par le système (`/usr/bin/python3`) souvent absent ou trop ancien selon la version — installer plutôt la version officielle depuis [python.org](https://www.python.org/downloads/macos/) ou via Homebrew : `brew install python`.
  - `pip install -r requirements.txt` peut prendre nettement plus de temps qu'ailleurs : pywebview installe `pyobjc` (bindings Objective-C) sur macOS, plus volumineux à récupérer/compiler — normal, pas une erreur.

```bash
git clone https://github.com/heyounana/depharass.git
cd depharass

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 app.py
```

### Construire un exécutable autonome (optionnel)

Pour distribuer l'app à quelqu'un sans Python installé. PyInstaller ne fait pas de cross-compilation : lancer la commande sur la plateforme cible (un build Linux ne donne pas d'exécutable macOS ni l'inverse ; le build Windows est en section suivante). Les commandes Linux et macOS diffèrent par un flag — voir pourquoi ci-dessous.

**Linux :**

```bash
pip install -r requirements-build.txt
pyinstaller --onefile --windowed --name dep_harass --clean \
    --collect-all webview --add-data "web:web" --add-data "data:data" app.py
```

Résultat : `dist/dep_harass`, un seul fichier exécutable — `chmod +x dist/dep_harass` puis le distribuer tel quel, rien d'autre à garder à côté.

`--onefile` plutôt que `--onedir` ici : sur Linux il n'existe pas de format d'application "bundle" comme sur macOS, donc `--onedir` produirait un dossier (exécutable + `_internal/`) à garder assemblé pour le distribuer — `--onefile` évite complètement cette contrainte. Contrepartie : l'exécutable se désarchive dans un dossier temporaire **à chaque lancement**, pas seulement au premier, donc un démarrage un peu plus lent qu'avec `--onedir`. L'argument antivirus qui justifie `--onedir` sous Windows (heuristiques associant l'auto-extraction au runtime au comportement d'un dropper) ne s'applique pas ici — Linux n'a pas d'équivalent d'antivirus grand public à contourner.

**macOS :**

```bash
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name dep_harass --clean \
    --collect-all webview --add-data "web:web" --add-data "data:data" app.py
```

Résultat : `dist/dep_harass.app`, un bundle applicatif standard macOS (Finder l'affiche comme une seule icône, malgré `--onedir` — `--windowed` place systématiquement le contenu, `_internal` compris, à l'intérieur du bundle plutôt qu'à côté). **Distribuer le `.app` entier.**

`--onedir` plutôt que `--onefile` ici, à l'inverse de Linux : la documentation officielle de PyInstaller déconseille explicitement `--onefile --windowed` sur macOS — un bundle onefile se réextrait *et* se refait rescanner par le système à chaque lancement (pas seulement le premier), et n'est de toute façon pas compatible avec le sandboxing requis pour une distribution via le Mac App Store. Comme `--windowed` produit déjà un bundle unique avec `--onedir`, `--onefile` n'apporterait ici aucun bénéfice de distribution, seulement ces inconvénients.

Sous macOS, un bundle non signé/notarié déclenche Gatekeeper (« ne peut pas être ouvert car il provient d'un développeur non identifié ») au premier lancement : clic droit sur `dep_harass.app` → *Ouvrir*, ou dans un terminal `xattr -d com.apple.quarantine dep_harass.app`. Même situation que SmartScreen sous Windows ci-dessous — seule une signature de code (compte développeur Apple payant) l'évite complètement.

Commun aux deux commandes :

- `--collect-all webview` : pywebview charge son backend par plateforme dynamiquement (WebView2, GTK, Qt...), invisible à l'analyse statique de PyInstaller sans ce flag.
- `--add-data` embarque `web/` (page + assets Quill) et `data/` (CSV des députés), qui ne sont pas du code Python.

## Installation et lancement — Windows

```cmd
git clone https://github.com/heyounana/depharass.git
cd depharass

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python app.py
```

(Si `python` n'est pas reconnu, essayer `py` à la place.)

### Construire un exécutable autonome (optionnel)

Pour distribuer l'app à quelqu'un sans Python installé. Le build doit se faire **sur Windows** — PyInstaller ne fait pas de cross-compilation.

```cmd
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name dep_harass --clean ^
    --collect-all webview --add-data "web;web" --add-data "data;data" app.py
```

Comme sous macOS (section précédente), `--onedir` plutôt que `--onefile` : démarrage quasi instantané (pas d'auto-extraction vers un dossier temporaire à chaque lancement) et nettement moins de faux positifs antivirus, ce dernier point étant justement le comportement runtime que les heuristiques Windows associent aux droppers — contrairement à Linux, cet argument s'applique pleinement ici. `--collect-all webview` et `--add-data` répondent aux mêmes raisons que sous Linux/macOS (voir ci-dessus) ; seul le séparateur de `--add-data` change (`;` au lieu de `:`).

Résultat : `dist\dep_harass\`, avec `dep_harass.exe` dedans. **Distribuer le dossier entier**, pas l'exécutable seul — il dépend des fichiers à côté de lui (`_internal\`). L'exécutable n'étant pas signé, il peut déclencher un avertissement SmartScreen au premier lancement (« éditeur non reconnu ») ; c'est indépendant du build, seule une signature de code payante l'évite.

## Publier une release

Le fichier produit par le build (`dep_harass.exe`, le bundle `dep_harass.app`, ou le binaire `dep_harass` sous Linux — zippé/tar.gz selon la plateforme) se publie en asset d'une release GitHub, pas dans le dépôt git : `dist/` est volontairement ignoré, un binaire committé resterait dans l'historique pour toujours.

```bash
git tag v1.0
git push origin v1.0
gh release create v1.0 dist/dep_harass.zip --title "dep_harass v1.0"
```

Le lien de téléchargement référencé par le README suit alors le format
`https://github.com/heyounana/depharass/releases/download/<tag>/dep_harass.zip`.
