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

## Installation et lancement — Linux

```bash
git clone https://github.com/heyounana/depharass.git
cd depharass

# backend GTK (le plus leger ; le paquet Qt ci-dessous est un plan B)
sudo apt install python3-gi gir1.2-webkit2-4.1
# si GTK indisponible/echoue au lancement, Qt en remplacement :
#   pip install qtpy pyqt6 PyQt6-WebEngine

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python app.py
```

### Construire un exécutable autonome (optionnel)

Pour distribuer l'app à quelqu'un sans Python installé. Valable pour Linux **et macOS** (mêmes commandes) — PyInstaller ne fait pas de cross-compilation, ce build ne produit pas de `.exe` Windows, il faut le faire depuis une machine Windows pour ça (section Windows plus bas), ni un exécutable macOS depuis Linux (section macOS juste en dessous).

```bash
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name dep_harass --clean \
    --collect-all webview --add-data "web:web" --add-data "data:data" app.py
```

- `--onedir` plutôt que `--onefile` : démarrage quasi instantané (pas d'auto-extraction vers un dossier temporaire à chaque lancement) et nettement moins de faux positifs antivirus, ce dernier point étant justement le comportement runtime que les heuristiques associent aux droppers.
- `--collect-all webview` : pywebview charge son backend par plateforme dynamiquement (WebView2, GTK, Qt...), invisible à l'analyse statique de PyInstaller sans ce flag.
- `--add-data` embarque `web/` (page + assets Quill) et `data/` (CSV des députés), qui ne sont pas du code Python.

Résultat : `dist/dep_harass/`. **Distribuer le dossier entier**, pas l'exécutable seul — il dépend des fichiers à côté de lui (`_internal/`).

## Installation et lancement — macOS

```bash
git clone https://github.com/heyounana/depharass.git
cd depharass

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 app.py
```

- **Python** : celui fourni par macOS (`/usr/bin/python3`) est souvent absent ou trop ancien selon la version du système — installer plutôt la version officielle depuis [python.org](https://www.python.org/downloads/macos/) ou via Homebrew : `brew install python`.
- **WKWebView** est natif au système (cf. Prérequis) : rien à installer côté moteur de rendu.
- `pip install -r requirements.txt` peut prendre nettement plus de temps ici que sous Linux/Windows : pywebview installe `pyobjc` (bindings Objective-C) sur macOS, plus volumineux à récupérer/compiler — c'est normal, pas une erreur.

### Construire un exécutable autonome (optionnel)

Même commande que Linux ci-dessus (section précédente), à lancer **sur macOS** — PyInstaller ne fait pas de cross-compilation, un build Linux ne donne pas d'exécutable macOS.

Résultat : `dist/dep_harass/`. Sous macOS, un exécutable non signé/notarié déclenche Gatekeeper (« ne peut pas être ouvert car il provient d'un développeur non identifié ») au premier lancement : clic droit sur `dep_harass` → *Ouvrir*, ou dans un terminal, depuis le dossier extrait, `xattr -d com.apple.quarantine dep_harass`. Même situation que SmartScreen sous Windows (section suivante) — seule une signature de code (compte développeur Apple payant) l'évite complètement.

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

Seule différence avec la commande Linux/macOS : le séparateur de `--add-data` (`;` au lieu de `:`). Le reste des options (`--onedir`, `--collect-all webview`, etc.) répond aux mêmes raisons, détaillées dans la section Linux ci-dessus.

Résultat : `dist\dep_harass\`, avec `dep_harass.exe` dedans. **Distribuer le dossier entier**, pas l'exécutable seul — il dépend des fichiers à côté de lui (`_internal\`). L'exécutable n'étant pas signé, il peut déclencher un avertissement SmartScreen au premier lancement (« éditeur non reconnu ») ; c'est indépendant du build, seule une signature de code payante l'évite.

## Publier une release

Le `.zip` produit par le build se publie en asset d'une release GitHub (pas dans
le dépôt git : `dist/` est volontairement ignoré, un binaire committé resterait
dans l'historique pour toujours).

```bash
git tag v1.0
git push origin v1.0
gh release create v1.0 dist/dep_harass.zip --title "dep_harass v1.0"
```

Le lien de téléchargement référencé par le README suit alors le format
`https://github.com/heyounana/depharass/releases/download/<tag>/dep_harass.zip`.
