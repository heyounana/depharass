# depharass

Application de bureau (fenêtre unique, [pywebview](https://pywebview.flowrl.com/)) pour l'envoi de mails en masse via SMTP — détection automatique du serveur, retry/backoff sur les erreurs temporaires, personnalisation par destinataire ({{FIRST}}, {{LAST}}, {{TITLE}}, {{TERM}}), éditeur HTML intégré (Quill), et chargement des députés de l'Assemblée nationale par groupe politique.

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

## Note sur le mot de passe

Le champ « Mot de passe » attend un **mot de passe d'application**, pas le mot de passe habituel du compte (Gmail et la plupart des fournisseurs l'exigent dès que la validation en deux étapes est active — à générer depuis les paramètres de sécurité du compte).

## Construire un exécutable autonome (PyInstaller)

Optionnel — pour distribuer l'app à quelqu'un sans Python installé. Le build doit se faire **sur la plateforme cible** : PyInstaller ne fait pas de cross-compilation, un build Linux produit un binaire Linux, pas un `.exe` Windows.

### Linux / macOS

```bash
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name send_mail_app --clean \
    --collect-all webview --add-data "web:web" --add-data "data:data" app.py
```

### Windows

```cmd
pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name send_mail_app --clean ^
    --collect-all webview --add-data "web;web" --add-data "data;data" app.py
```

Seule différence entre les deux : le séparateur de `--add-data` (`:` sous Linux/macOS, `;` sous Windows).

- `--onedir` plutôt que `--onefile` : démarrage quasi instantané (pas d'auto-extraction vers un dossier temporaire à chaque lancement) et nettement moins de faux positifs antivirus, ce dernier point étant justement le comportement runtime que les heuristiques associent aux droppers.
- `--collect-all webview` : pywebview charge son backend par plateforme dynamiquement (WebView2, GTK, Qt...), invisible à l'analyse statique de PyInstaller sans ce flag.
- `--add-data` embarque `web/` (page + assets Quill) et `data/` (CSV des députés), qui ne sont pas du code Python.

Résultat : `dist/send_mail_app/`. **Distribuer le dossier entier**, pas l'exécutable seul — il dépend des fichiers à côté de lui (`_internal/`). Sous Windows, l'exécutable non signé déclenchera un avertissement SmartScreen au premier lancement (« éditeur non reconnu ») ; c'est indépendant du build, seule une signature de code payante l'évite.
