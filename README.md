# depharass

Application de bureau (fenêtre unique, [pywebview](https://pywebview.flowrl.com/)) pour l'envoi de mails en masse via SMTP

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

Pour distribuer l'app à quelqu'un sans Python installé. Valable pour Linux **et macOS** (mêmes commandes) — PyInstaller ne fait pas de cross-compilation, ce build ne produit pas de `.exe` Windows, il faut le faire depuis une machine Windows pour ça (section suivante).

```bash
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name dep_harass --clean \
    --collect-all webview --add-data "web:web" --add-data "data:data" app.py
```

- `--onedir` plutôt que `--onefile` : démarrage quasi instantané (pas d'auto-extraction vers un dossier temporaire à chaque lancement) et nettement moins de faux positifs antivirus, ce dernier point étant justement le comportement runtime que les heuristiques associent aux droppers.
- `--collect-all webview` : pywebview charge son backend par plateforme dynamiquement (WebView2, GTK, Qt...), invisible à l'analyse statique de PyInstaller sans ce flag.
- `--add-data` embarque `web/` (page + assets Quill) et `data/` (CSV des députés), qui ne sont pas du code Python.

Résultat : `dist/dep_harass/`. **Distribuer le dossier entier**, pas l'exécutable seul — il dépend des fichiers à côté de lui (`_internal/`).

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

## Note sur le mot de passe

Le champ « Mot de passe » attend un **mot de passe d'application**, pas le mot de passe habituel du compte — Gmail et la plupart des fournisseurs l'exigent dès que la validation en deux étapes est active.

### Procédure Gmail

1. Activer la validation en deux étapes si ce n'est pas déjà fait, depuis [myaccount.google.com/security](https://myaccount.google.com/security) — un mot de passe d'application ne peut pas être généré sans elle.
2. Aller sur [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (ou chercher « Mots de passe des applications » dans la recherche des paramètres du compte).
3. Donner un nom (ex. `depharass`) et valider.
4. Google affiche un mot de passe de 16 caractères en 4 blocs de 4 (ex. `abcd efgh ijkl mnop`) : le copier tel quel — les espaces sont retirés automatiquement par l'app, inutile de les enlever à la main.
5. Coller ce mot de passe dans le champ « Mot de passe » de l'app, avec l'adresse Gmail correspondante dans « De ».

Compte **Google Workspace** (pro/asso) : cette option peut être désactivée par l'administrateur du domaine ; le cas échéant, elle n'apparaît pas dans les paramètres de sécurité et il faut la demander à l'administrateur ou utiliser un compte Gmail personnel.

Pour un autre fournisseur (Outlook, Yahoo, etc.), le principe est identique — un mot de passe d'application se génère depuis les paramètres de sécurité du compte, une fois la validation en deux étapes active — seul l'emplacement du réglage change.
