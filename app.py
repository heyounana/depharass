#!/usr/bin/env python3
"""Application a fenetre unique (pywebview) pour l'envoi de mails.

Toute l'interface est une page HTML/CSS/JS rendue dans la webview native du
systeme (WebView2 sur Windows, WebKitGTK sur Linux, WKWebView sur macOS) :
pas de Chromium embarque, contrairement a Electron.

La logique metier (detection SMTP, retry/backoff 4xx, redecoupe des lots sur
452, personnalisation {{FIRST}}/{{LAST}}) vit entierement dans send_mail.py et
n'est pas dupliquee ici. Ce module ne fait que l'exposer a la page via le pont
pywebview : cote JS, window.pywebview.api.<methode>() renvoie une Promise.

Lancement : python app.py
"""
import csv
import functools
import json
import smtplib
import sys
import threading
from pathlib import Path

import webview

import send_mail as sm

# pywebview >= 5 expose l'enum FileDialog ; OPEN_DIALOG reste dispo mais est
# deprecie (avertissement a l'import en 6.x) et disparaitra.
OPEN_DIALOG = getattr(webview, "FileDialog", None)
OPEN_DIALOG = OPEN_DIALOG.OPEN if OPEN_DIALOG else webview.OPEN_DIALOG


def _base_dir():
    """Racine des assets embarques, y compris une fois fige par PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def web_dir():
    return _base_dir() / "web"


def data_dir():
    return _base_dir() / "data"


@functools.lru_cache(maxsize=1)
def _load_deputies():
    """Parse data/deputees.csv (Prenom,Nom,Email,Groupe (sigle),Groupe
    (libelle),...). Mis en cache : le fichier est statique pour la duree de
    vie de l'app, pas besoin de le reparser a chaque clic. Renvoie [] si le
    fichier est absent plutot que lever — c'est une fonctionnalite optionnelle,
    son absence ne doit pas gener le reste de l'app."""
    path = data_dir() / "deputees.csv"
    if not path.is_file():
        return []
    texte = sm.read_text_tolerant(str(path))
    out = []
    for row in csv.DictReader(texte.splitlines()):
        email = (row.get("Email") or "").strip()
        if not sm.ADDR_RE.match(email):
            continue  # ligne mal formee, ne casse pas le chargement du reste
        out.append({
            "prenom": (row.get("Prénom") or "").strip(),
            "nom": (row.get("Nom") or "").strip(),
            "genre": (row.get("Genre") or "").strip().upper(),
            "email": email,
            "sigle": (row.get("Groupe (sigle)") or "").strip(),
            "libelle": (row.get("Groupe (libellé)") or "").strip(),
        })
    return out


def _deputy_genders():
    """Table {adresse en minuscules: "M"/"F"} pour {{TITLE}}/{{TERM}}.

    Seuls les deputes ont un genre connu : il vient du fichier, jamais d'une
    deduction sur le prenom ou l'adresse. Une adresse saisie a la main reste
    donc sans genre, et l'utilisateur en est averti plutot que de recevoir un
    accord grammatical invente."""
    return {d["email"].lower(): d["genre"]
            for d in _load_deputies() if d["genre"] in ("M", "F")}


class _StderrRelay:
    """Redirige ce que send_mail.py ecrit sur stderr (detection SMTP, retries
    4xx, redecoupe des lots) vers le journal de la page. Sans ca ces messages
    seraient perdus : une app fenetree n'a pas de console rattachee."""

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            ligne, self._buf = self._buf.split("\n", 1)
            if ligne.strip():
                self._emit(ligne.rstrip())

    def flush(self):
        pass


class Api:
    """Methodes appelables depuis la page via window.pywebview.api."""

    def __init__(self):
        self.window = None
        self._sending = False

    # ------------------------------------------------------------- interne

    def _emit(self, kind, payload=None):
        """Pousse un evenement vers la page (handler onAppEvent cote JS)."""
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                f"window.onAppEvent({json.dumps(kind)}, {json.dumps(payload)})")
        except Exception:
            pass  # fenetre fermee pendant un envoi

    def _prepare(self, p):
        """Valide les champs et construit la config d'envoi.
        Leve ValueError (champ invalide) ou sm.SendMailError (domaine SMTP)."""
        sender = (p.get("sender") or "").strip()
        if not sm.ADDR_RE.match(sender):
            raise ValueError(f"adresse expeditrice invalide : {sender or '(vide)'}")

        # Chaque ligne vaut "adresse" ou "adresse,T" (T = H/M/F, casse libre).
        # Le titre ecrit sur la ligne est la reference : il est visible et
        # modifiable a la main dans le champ, contrairement a l'ancien etat
        # cache cote page.
        dests = []
        titres_lignes = {}
        for ligne in (p.get("dests") or []):
            if not ligne.strip():
                continue
            adresse, genre = sm.split_recipient(ligne)
            if not sm.ADDR_RE.match(adresse):
                raise ValueError(f"adresse destinataire invalide : {adresse}")
            dests.append(adresse)
            if genre:
                titres_lignes[adresse.lower()] = genre
        dests = list(dict.fromkeys(dests))  # dedoublonne en gardant l'ordre
        if not dests:
            raise ValueError("aucun destinataire")

        subject = (p.get("subject") or "").strip()
        if not subject:
            raise ValueError("objet manquant")

        body = p.get("body") or ""
        if not body.strip():
            raise ValueError("corps du mail vide")

        group = bool(p.get("group"))
        if group and sm.has_placeholders(body):
            raise ValueError("les placeholders ({{FIRST}}, {{TITLE}}...) "
                             "necessitent un mail par destinataire — decoche "
                             "Grouper")

        # `or <defaut>` serait piegeux ici : 0 est falsy, un lot de 0 passerait
        # silencieusement a 50. On distingue explicitement "champ vide" de "0".
        brut = p.get("batchSize")
        if brut in (None, ""):
            batch = 50
        else:
            try:
                batch = int(brut)
            except (TypeError, ValueError):
                raise ValueError("taille de lot invalide")
        if batch < 1:
            raise ValueError("taille de lot invalide")

        brut = p.get("smtpPort")
        if brut in (None, ""):
            port = None
        else:
            try:
                port = int(brut)
            except (TypeError, ValueError):
                raise ValueError("port SMTP invalide")
            if not 1 <= port <= 65535:
                raise ValueError("port SMTP invalide")

        host, resolved_port = sm.resolve_smtp(
            sender, (p.get("smtpHost") or "").strip() or None, port)

        lots = ([dests[i:i + batch] for i in range(0, len(dests), batch)]
                if group else [[d] for d in dests])

        # Un titre ecrit sur la ligne prime sur le CSV des deputes : c'est un
        # choix explicite et visible de l'utilisateur. Le CSV reste le repli
        # pour une adresse de depute saisie sans titre, qui garde ainsi le
        # sien sans qu'on ait a le retaper.
        genres = {**_deputy_genders(), **titres_lignes}
        sans_genre = 0
        if sm.has_gender_placeholders(body):
            sans_genre = sum(1 for d in dests if d.lower() not in genres)

        return {
            "sender": sender, "host": host, "port": resolved_port,
            "subject": subject, "body": body,
            "is_html": bool(p.get("isHtml")), "lots": lots,
            "genres": genres, "sans_genre": sans_genre,
        }

    # -------------------------------------------------------------- exposees

    def pick_recipients_file(self):
        """Dialogue natif de selection d'un fichier d'adresses."""
        chemins = self.window.create_file_dialog(
            OPEN_DIALOG, allow_multiple=False,
            file_types=("Fichiers de contacts (*.txt;*.csv;*.tsv;*.json)",
                        "Tous les fichiers (*.*)"))
        if not chemins:
            return {"addresses": []}
        try:
            texte = sm.read_text_tolerant(chemins[0])
        except OSError as e:
            return {"error": f"lecture impossible : {e}"}
        adresses, titres, stats = sm.scan_addresses(texte)
        if not adresses:
            return {"error": "aucune adresse email trouvee dans ce fichier"}
        return {"addresses": adresses, "titles": titres, "isJson": stats["json"],
                "ignored": stats["ignorees"], "multi": stats["multiples"]}

    def load_deputy_data(self):
        """Groupes (avec effectif) ET liste complete (email, sigle) des deputes.

        Tout part en une fois plutot qu'un appel par filtrage : la page peut
        alors reagir instantanement a chaque case cochee/decochee, sans
        aller-retour vers Python. 577 entrees, la charge est negligeable.
        La page a de toute facon besoin de la table email -> groupe pour
        savoir quelles lignes du champ Destinataires lui appartiennent et
        peuvent etre retirees, sans toucher aux adresses saisies a la main."""
        deputes = _load_deputies()
        if not deputes:
            return {"error": f"fichier introuvable : {data_dir() / 'deputees.csv'}"}
        groupes = {}
        for d in deputes:
            g = groupes.setdefault(d["sigle"], {"sigle": d["sigle"],
                                                 "libelle": d["libelle"], "count": 0})
            g["count"] += 1
        return {
            "groups": sorted(groupes.values(), key=lambda g: -g["count"]),
            # genre necessaire ici : la page ecrit les lignes au format
            # "adresse,M" / "adresse,F" dans le champ Destinataires.
            "deputies": [{"email": d["email"], "sigle": d["sigle"],
                          "genre": d["genre"]} for d in deputes],
            "total": len(deputes),
        }

    def dry_run(self, p):
        """Simule : resout le serveur, verifie reellement l'authentification
        (connexion + login, sans envoyer aucun mail — la session est fermee
        aussitot), construit les lots, rend le corps personnalise pour le
        premier destinataire. Un mot de passe errone est ainsi detecte avant
        un envoi reel, pas pendant."""
        relay = _StderrRelay(lambda ligne: self._emit("log", ligne))
        ancien, sys.stderr = sys.stderr, relay
        try:
            try:
                cfg = self._prepare(p)
            except (ValueError, sm.SendMailError) as e:
                return {"error": str(e)}

            pwd = "".join((p.get("password") or "").split())
            if not pwd:
                return {"error": "mot de passe manquant"}

            try:
                conn = sm.connect(cfg["host"], cfg["port"], cfg["sender"], pwd)
            except smtplib.SMTPAuthenticationError as e:
                return {"error": sm.auth_error_msg(cfg["host"], cfg["sender"], e)}
            except (smtplib.SMTPException, OSError) as e:
                return {"error": f"connexion a {cfg['host']}:{cfg['port']} echouee : {e}"}
            except Exception as e:
                return {"error": f"erreur inattendue a la connexion : {e!r}"}
            try:
                conn.quit()
            except smtplib.SMTPException:
                pass
        finally:
            sys.stderr = ancien

        premier = cfg["lots"][0][0]
        corps = cfg["body"]
        personnalise = sm.has_placeholders(corps)
        if personnalise:
            corps = sm.personalize(corps, premier, cfg["genres"].get(premier.lower()))
        return {
            "host": cfg["host"], "port": cfg["port"],
            "lots": [list(lot) for lot in cfg["lots"]],
            "body": corps, "personalizedFor": premier if personnalise else None,
            "isHtml": cfg["is_html"], "missingGender": cfg["sans_genre"],
        }

    def send(self, p):
        """Lance l'envoi dans un thread ; la progression revient par
        onAppEvent (log / error / done)."""
        if self._sending:
            return {"error": "un envoi est deja en cours"}
        try:
            cfg = self._prepare(p)
        except (ValueError, sm.SendMailError) as e:
            return {"error": str(e)}

        pwd = "".join((p.get("password") or "").split())
        if not pwd:
            return {"error": "mot de passe manquant"}

        self._sending = True
        threading.Thread(target=self._send_worker, args=(cfg, pwd),
                         daemon=True).start()
        return {"started": True,
                "lots": len(cfg["lots"]),
                "dests": sum(len(lot) for lot in cfg["lots"]),
                "missingGender": cfg["sans_genre"]}

    # ---------------------------------------------------- thread d'envoi

    def _send_worker(self, cfg, pwd):
        sender, host, port = cfg["sender"], cfg["host"], cfg["port"]
        relay = _StderrRelay(lambda ligne: self._emit("log", ligne))
        ancien, sys.stderr = sys.stderr, relay

        def connect_fn():
            return sm.connect(host, port, sender, pwd)

        try:
            try:
                conn = [connect_fn()]
            except smtplib.SMTPAuthenticationError as e:
                self._emit("error", sm.auth_error_msg(host, sender, e))
                return
            except (smtplib.SMTPException, OSError) as e:
                self._emit("error", f"connexion a {host}:{port} echouee : {e}")
                return
            except Exception as e:
                # Filet de securite : meme une erreur totalement imprevue au
                # moment de se connecter doit atterrir dans le journal de la
                # page, jamais disparaitre — une app --windowed n'a pas de
                # console ou une traceback silencieuse serait visible.
                self._emit("error", f"erreur inattendue a la connexion : {e!r}")
                return

            ok_total, echecs_total = [], []
            try:
                for lot in cfg["lots"]:
                    ok, echecs = sm.send_lot(conn, connect_fn, sender, lot,
                                              cfg["subject"], cfg["body"],
                                              cfg["is_html"],
                                              sm.RETRY_MAX_ATTEMPTS,
                                              cfg["genres"])
                    if ok:
                        self._emit("ok", ok)
                    for addr, detail in echecs:
                        self._emit("fail", {"addr": addr,
                                             "detail": sm.fmt_smtp(detail)})
                    ok_total += ok
                    echecs_total += echecs
            except smtplib.SMTPAuthenticationError as e:
                # reconnexion en cours de route refusee
                self._emit("error", sm.auth_error_msg(host, sender, e))
            except Exception as e:
                # Meme filet de securite pendant l'envoi : send_lot() gere
                # deja les cas SMTP/reseau connus (voir send_mail.py), mais
                # si quelque chose d'imprevu passe au travers, ca doit quand
                # meme finir dans le journal plutot que de tuer le thread en
                # silence — les lots restants du batch sont alors abandonnes.
                self._emit("error", f"erreur inattendue pendant l'envoi : {e!r}")
            finally:
                try:
                    conn[0].quit()
                except smtplib.SMTPException:
                    pass

            self._emit("summary", {"ok": len(ok_total), "ko": len(echecs_total)})
        finally:
            sys.stderr = ancien
            self._sending = False
            self._emit("done")


def main():
    index = web_dir() / "index.html"
    if not index.is_file():
        sys.exit(f"page introuvable : {index}")

    # Hauteur d'ouverture = hauteur minimale. 870px mesure comme le seuil
    # reel a partir duquel #form-grid tient sans son propre scroll interne
    # (marge de securite incluse) UNE FOIS le panneau "Charger les deputes"
    # deplie — pas juste le formulaire nu (720px suffisait pour ca seul,
    # mais laissait #form-grid trop court des que ce panneau apparait, avec
    # #deputy-groups-list borne a 100px cote CSS). Consequence acceptee :
    # un peu de vide sous "Options avancees" quand ce panneau n'est jamais
    # ouvert — prefere a toute logique de redimensionnement dynamique
    # (essayee, source de plusieurs bugs d'ordonnancement JS/Python,
    # abandonnee au profit de cette seule valeur statique).
    api = Api()
    window = webview.create_window(
        "Envoi de mail", str(index), js_api=api,
        width=1060, height=870, min_size=(820, 870))

    # Sous Windows (backend WebView2/WinForms), exposer tout de suite la
    # reference a la fenetre sur l'objet js_api declenche un bug connu de
    # pywebview : sa passe d'introspection de l'objet js_api parcourt
    # recursivement window.native, dont le graphe d'objets .NET est cyclique
    # par nature (AccessibilityObject.Owner, SyncRoot, FontFamily.
    # GenericSansSerif se referencent eux-memes), ce qui declenche un
    # RecursionError en boucle et inonde la console de lignes
    # "[pywebview] Error while processing window.native....." (inoffensif
    # pour la fenetre elle-meme, mais tres bruyant, cf.
    # github.com/r0x0r/pywebview/issues/1815). Le contournement confirme est
    # de ne renseigner l'attribut qu'apres le chargement de la page, une
    # fois cette passe d'introspection initiale deja terminee — aucune
    # methode de Api n'utilise self.window avant un clic utilisateur, donc
    # rien ne peut l'appeler avant que "loaded" se soit declenche.
    window.events.loaded += lambda: setattr(api, "window", window)

    try:
        # La page est chargee en file:// : aucun socket ouvert, donc pas de
        # pare-feu/antivirus a reveiller. Si WebView2 refusait de charger les
        # assets locaux (page blanche, Quill absent), passer a :
        #     webview.start(http_server=True)
        # pywebview sert alors web/ via un serveur local interne.
        #
        # Sous Linux, pywebview essaie GTK avant Qt et affiche une traceback
        # (juste bruyante, pas fatale) si les bindings GTK (module 'gi')
        # sont absents. On force Qt directement pour l'eviter, puisque
        # Windows/macOS ne passent de toute facon jamais par ce choix
        # (WebView2/WKWebView natifs, choisis automatiquement).
        gui = "qt" if sys.platform.startswith("linux") else None
        webview.start(gui=gui)
    except Exception as e:
        # Moteur de rendu introuvable. Message actionnable plutot qu'une
        # stacktrace, d'autant qu'une app --windowed n'a pas de console.
        if sys.platform == "win32":
            remede = ("installe le runtime WebView2 depuis "
                      "https://developer.microsoft.com/microsoft-edge/webview2/ "
                      "(section 'Evergreen Standalone Installer'), puis relance.")
        elif sys.platform == "darwin":
            remede = "aucun moteur WKWebView disponible — mets macOS a jour."
        else:
            # Attention au piege cote Qt : pywebview importe qtpy (couche
            # d'abstraction), pas PyQt directement — installer pyqt6 seul ne
            # suffit pas. Et le moteur web de PyQt6 est PyQt6-WebEngine, pas
            # pyqtwebengine (qui est la variante PyQt5 et tire tout PyQt5).
            remede = ("installe un backend graphique, au choix :\n"
                      "     GTK : sudo apt install python3-gi gir1.2-webkit2-4.1\n"
                      "     Qt  : pip install qtpy pyqt6 PyQt6-WebEngine")
        sys.exit(f"impossible d'ouvrir la fenetre : {e}\n  -> {remede}")


if __name__ == "__main__":
    main()
