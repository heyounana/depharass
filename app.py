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
import random
import re
import smtplib
import sys
import threading
import time
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


# Precision entre parentheses ajoutee au nom pour lever une ambiguite dans un
# tableau ("Martin (Alpes-Maritimes)") : utile en colonne, pas dans une formule
# d'appel — "Bonjour Mme. MARTIN (ALPES-MARITIMES)," serait pire que tout.
_PRECISION_NOM = re.compile(r"\s*\([^)]*\)\s*$")


@functools.lru_cache(maxsize=1)
def _deputy_names():
    """(table, partagees) — noms des deputes, et adresses problematiques.

    table : {adresse en minuscules: (prenom, nom)}, source faisant autorite
    pour {{FIRST}}/{{LAST}}. Une adresse que le CSV attribue a deux deputes
    portant des noms differents en est retiree : choisir arbitrairement ferait
    partir un mail au mauvais nom.

    partagees : les adresses presentes sur plusieurs lignes du CSV, meme quand
    le nom est identique. C'est un defaut du fichier, pas de l'envoi : deux
    elues distinctes y partagent alexandra.martin@, donc l'une des deux ne sera
    jamais contactee et l'autre recevra deux fois le meme message. Rien ici ne
    peut le corriger — seulement le signaler pour que le CSV soit repare."""
    par_adresse, lignes = {}, {}
    for d in _load_deputies():
        addr = d["email"].lower()
        lignes[addr] = lignes.get(addr, 0) + 1
        nom = _PRECISION_NOM.sub("", d["nom"]).strip()
        if nom and d["prenom"]:
            par_adresse.setdefault(addr, set()).add((d["prenom"], nom))
    table = {a: noms.pop() for a, noms in par_adresse.items() if len(noms) == 1}
    partagees = sorted(a for a, n in lignes.items() if n > 1)
    return table, partagees


# Quotas d'envoi quotidiens connus, par domaine expediteur. Table distincte de
# sm.PROVIDERS, qui sert a trouver le serveur SMTP et repond a une autre
# question : un domaine peut y figurer sans quota connu, et l'inverse.
QUOTAS = {"gmail.com": 500, "googlemail.com": 500}

UNITES_DUREE = {"minutes": 60, "heures": 3600, "jours": 86400}


def _duree_secondes(valeur, unite):
    """Duree de campagne en secondes. Vide -> 0, c'est-a-dire aucun etalement
    (l'ancien comportement) ; le preflight le signale plutot que de l'imposer."""
    if valeur in (None, ""):
        return 0.0
    try:
        n = float(valeur)
    except (TypeError, ValueError):
        raise ValueError("duree de campagne invalide")
    if n < 0:
        raise ValueError("duree de campagne invalide")
    return n * UNITES_DUREE.get(unite, 3600)


def _minutes_jour(valeur, champ):
    """"HH:MM" -> minutes depuis minuit, ou None si le champ est vide."""
    if not valeur:
        return None
    try:
        h, m = str(valeur).split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        raise ValueError(f"{champ} : heure invalide ({valeur!r}), format attendu HH:MM")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"{champ} : heure invalide ({valeur!r})")
    return h * 60 + m


def _plage_horaire(debut, fin):
    """(debut, fin) en minutes depuis minuit, ou None si aucune restriction.

    Hors de cette plage la campagne se met en pause d'elle-meme : distribuer
    des mails a 3 h du matin est une des heuristiques anti-bot les moins
    cheres qui soient."""
    d = _minutes_jour(debut, "debut de plage")
    f = _minutes_jour(fin, "fin de plage")
    if d is None or f is None or d == f:
        return None
    return (d, f)


def planifier(cfg):
    """Ecarts entre les envois de cette config.

    Point de calcul unique de la cadence : le preflight l'annonce, la
    simulation l'affiche et le worker la suit — les trois doivent parler du
    meme rythme. Redériver la formule ailleurs l'a deja fait diverger (un
    ecart moyen negatif annonce dans le preflight).

    Travaille sur le nombre de LOTS, ce qui vaut pour les deux modes : un lot
    groupe part en un seul message, c'est donc lui qu'on cadence."""
    return sm.gaps(len(cfg["lots"]), cfg["duree"]) if cfg["duree"] else []


def _resume_campagne(cfg):
    """Chiffres et avertissements d'une campagne.

    Partage entre la simulation et la confirmation d'envoi : les deux doivent
    annoncer exactement les memes quotas, la meme cadence et les memes replis.
    Les regles restent ici, cote Python — app.js ne fait que les mettre en
    forme."""
    n = len(cfg["lots"])
    destinataires = sum(len(lot) for lot in cfg["lots"])
    duree = cfg["duree"]
    # Moyenne des ecarts reellement planifies, et non une reconstitution de la
    # formule : c'est ce que le worker appliquera.
    ecarts = planifier(cfg)
    ecart_moyen = sum(ecarts) / len(ecarts) if ecarts else 0.0

    avertissements = []
    quota = QUOTAS.get((cfg["sender"] or "").rsplit("@", 1)[-1].lower())
    if quota:
        # Le quota se compte en DESTINATAIRES, pas en messages : grouper 577
        # adresses en 12 envois n'en consomme pas 12 mais 577. Compter les lots
        # ici laisserait passer sans un mot une campagne groupee tres au-dessus
        # de la limite.
        #
        # La valeur (500/jour) vient de la seule page officielle Gmail sur le
        # sujet (support.google.com/mail/answer/22839), qui ne distingue pas
        # webmail et client SMTP — donc potentiellement optimiste pour un envoi
        # scripte comme celui-ci, sans qu'on ait de chiffre distinct a citer.
        # A ne pas durcir sans nouvelle source.
        jours = max(duree / 86400.0, 0.0)
        par_jour = destinataires / jours if jours >= 1 else destinataires
        if par_jour > quota:
            avertissements.append(
                f"{destinataires} destinataires depassent le quota de "
                f"{quota}/jour de ce fournisseur" +
                (f" ({par_jour:.0f}/jour au rythme prevu)" if jours >= 1 else "") +
                " — le quota se compte par destinataire, meme groupes dans un "
                "seul message. Au-dela, Google bloque l'envoi et indique un "
                "delai de 1 a 24 h avant de pouvoir reessayer (pas une duree "
                "garantie).")
    if n < 2:
        pass          # un seul message : ni cadence ni etalement a commenter
    elif not duree:
        avertissements.append(
            "aucune duree de campagne : les messages partiront a la suite, "
            "sans pause — c'est exactement ce qui fait bloquer un compte.")
    elif ecart_moyen < 20:
        avertissements.append(
            f"cadence rapide : un message toutes les {ecart_moyen:.0f} s en "
            f"moyenne. Allonger la duree reduit le risque de blocage.")
    if sm.has_placeholders(cfg["subject"] or ""):
        avertissements.append(
            "l'objet contient un placeholder ({{...}}) : il n'est pas "
            "personnalise et partira tel quel.")
    if cfg["partagees"]:
        avertissements.append(
            f"{len(cfg['partagees'])} adresse(s) designent plusieurs deputes "
            f"dans le CSV : l'un d'eux ne sera pas contacte "
            f"({', '.join(cfg['partagees'])}).")

    return {
        "messages": n,
        "destinataires": destinataires,
        "grouped": n != destinataires,
        "objets": len(cfg["subjects"]),
        "duree": duree,
        "ecartMoyen": ecart_moyen,
        "plage": list(cfg["plage"]) if cfg["plage"] else None,
        "approximations": cfg["approximations"],
        "warnings": avertissements,
        "ecarts": ecarts,
    }


def _prochaine_ouverture(ts, plage):
    """Avance `ts` jusqu'a la prochaine minute autorisee par la plage."""
    if not plage:
        return ts
    debut = plage[0]
    for _ in range(8):   # au pire quelques sauts de jour
        lt = time.localtime(ts)
        if dans_plage(plage, lt.tm_hour * 60 + lt.tm_min):
            return ts
        minuit = ts - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        cible = minuit + debut * 60
        ts = cible if cible > ts else cible + 86400
    return ts


def horaires_previsionnels(depart, ecarts, plage, nb, cout_envoi=sm.COUT_ENVOI):
    """Horodatages previsionnels des `nb` envois, plage horaire comprise.

    Rejoue ce que fera le worker — attendre l'ouverture de la plage, envoyer,
    patienter — mais sur une horloge simulee. Sert a montrer dans la
    simulation quand chaque message partira reellement : sur une campagne
    etalee sur des jours, "577 destinataires" ne dit rien d'utile, "le dernier
    part jeudi a 11h20" si."""
    horaires, ts = [], depart
    for i in range(nb):
        ts = _prochaine_ouverture(ts, plage)
        horaires.append(ts)
        ts += cout_envoi + (ecarts[i] if i < len(ecarts) else 0.0)
    return horaires


# Noms de jours en dur : la locale du systeme n'est pas garantie (elle est
# vide ici, et strftime("%A") rendrait "Saturday"), et elle varie d'une
# machine a l'autre — sur Windows notamment.
JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def _libelle_jour(ts):
    lt = time.localtime(ts)
    return f"{JOURS_FR[lt.tm_wday]} {lt.tm_mday:02d}/{lt.tm_mon:02d}"


def formate_horaires(horaires):
    """(heures, jours) pour affichage.

    `heures[i]` est toujours "HH:MM" en 24 h — les colonnes restent alignees.
    `jours[i]` ne porte un libelle que lorsque l'envoi i ouvre un jour
    different du precedent : la page intercale alors un separateur, plutot que
    de repeter la date sur chaque ligne et de desaligner la moitie de la
    liste. Le premier envoi n'est etiquete que s'il n'a pas lieu aujourd'hui."""
    if not horaires:
        return [], []
    heures = [time.strftime("%H:%M", time.localtime(t)) for t in horaires]

    jours, precedent = [], time.localtime().tm_yday
    for t in horaires:
        jour = time.localtime(t).tm_yday
        jours.append(_libelle_jour(t) if jour != precedent else "")
        precedent = jour
    return heures, jours


def dans_plage(plage, maintenant=None):
    """La plage autorise-t-elle un envoi maintenant ? Gere le cas d'une plage
    qui passe minuit (22:00-06:00)."""
    if not plage:
        return True
    debut, fin = plage
    t = maintenant if maintenant is not None else (
        time.localtime().tm_hour * 60 + time.localtime().tm_min)
    return debut <= t < fin if debut < fin else (t >= debut or t < fin)


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
        # Tout etat interne reste prefixe par "_" : pywebview n'expose a JS que
        # les attributs publics, et il parcourt recursivement ceux qui ne sont
        # pas appelables — c'est ce parcours qui provoquait la boucle infinie
        # sur window.native documentee dans main().
        self._sending = False
        self._cancel = threading.Event()
        self._pause = threading.Event()

    # ------------------------------------------------------------- interne

    def _gate(self):
        """Bloque tant que la campagne est en pause. False s'il faut arreter.

        Verifie inconditionnellement, y compris quand l'attente qui suit est
        nulle : sans ca, une pause demandee pile entre deux envois rapproches
        resterait sans effet."""
        while self._pause.is_set():
            if self._cancel.wait(0.2):
                return False
        return not self._cancel.is_set()

    def _wait(self, secondes):
        """Attente interruptible. False si la campagne doit s'arreter.

        Le restant est decremente du temps reellement ecoule (horloge
        monotone) et non du pas theorique : sur plusieurs heures, supposer que
        chaque tranche dure exactement 0,5 s derive de plusieurs dizaines de
        secondes. La pause bloque dans _gate(), donc en dehors de l'intervalle
        mesure : le temps passe en pause ne consomme pas l'attente."""
        reste = secondes
        while reste > 0:
            if not self._gate():
                return False
            debut = time.monotonic()
            if self._cancel.wait(min(reste, 0.5)):
                return False
            reste -= time.monotonic() - debut
        return self._gate()

    def _emit(self, kind, payload=None):
        """Pousse un evenement vers la page (handler onAppEvent cote JS)."""
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                f"window.onAppEvent({json.dumps(kind)}, {json.dumps(payload)})")
        except Exception:
            pass  # fenetre fermee pendant un envoi

    def _prepare(self, p, melanger=False, reseau=True):
        """Valide les champs et construit la config d'envoi.

        `melanger` n'est vrai que pour un envoi reel : la simulation et le
        preflight doivent rester reproductibles d'un clic a l'autre (meme
        ordre, meme apercu), et en mode groupe un melange changerait la
        composition des lots entre ce qui est annonce et ce qui part.

        `reseau=False` : planification seule (bouton Simuler). Ce qui ne sert
        qu'a l'envoi — expediteur, objet, corps — devient facultatif, et le
        serveur SMTP n'est pas resolu : resolve_smtp() ouvre de vraies
        connexions de test, or on veut pouvoir verifier un calendrier avec la
        seule liste de destinataires, sans reseau ni identifiants.

        Leve ValueError (champ invalide) ou sm.SendMailError (domaine SMTP)."""
        sender = (p.get("sender") or "").strip()
        # Un expediteur saisi mais errone reste une erreur, meme en mode
        # planification : le signaler tot vaut mieux qu'au moment d'envoyer.
        if (reseau or sender) and not sm.ADDR_RE.match(sender):
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
        # Dedoublonnage insensible a la casse : les tables de genres et de noms
        # sont toutes indexees en minuscules, et une fois l'ordre melange deux
        # variantes de casse de la meme adresse partiraient a des heures
        # differentes sans que personne ne remarque le doublon.
        vues, uniques = set(), []
        for d in dests:
            if d.lower() not in vues:
                vues.add(d.lower())
                uniques.append(d)
        dests = uniques
        if not dests:
            raise ValueError("aucun destinataire")

        # Objets : la page peut en fournir plusieurs (bouton "+"), auquel cas
        # chaque message tire le sien. Un objet stricttement identique sur des
        # centaines d'envois est un des marqueurs releves.
        subjects = [s.strip() for s in (p.get("subjects") or []) if s.strip()]
        if not subjects:
            unique = (p.get("subject") or "").strip()
            if not unique and reseau:
                raise ValueError("objet manquant")
            subjects = [unique] if unique else []
        subject = subjects[0] if subjects else ""

        # Un editeur Quill vide rend "<p><br></p>", que .strip() juge non vide :
        # jusqu'ici un corps HTML vierge passait donc la validation et partait
        # tel quel. On juge sur le texte rendu.
        body = p.get("body") or ""
        is_html = bool(p.get("isHtml"))
        corps_vide = not (sm.html_to_text(body) if is_html else body).strip()
        if corps_vide and reseau:
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

        # resolve_smtp() ouvre de vraies connexions de test pour deviner le
        # serveur : la planification seule n'en a pas besoin et ne doit pas
        # attendre le reseau.
        if reseau:
            host, resolved_port = sm.resolve_smtp(
                sender, (p.get("smtpHost") or "").strip() or None, port)
        else:
            host, resolved_port = None, port

        # Un titre ecrit sur la ligne prime sur le CSV des deputes : c'est un
        # choix explicite et visible de l'utilisateur. Le CSV reste le repli
        # pour une adresse de depute saisie sans titre, qui garde ainsi le
        # sien sans qu'on ait a le retaper.
        genres = {**_deputy_genders(), **titres_lignes}
        noms, partagees = _deputy_names()

        # Personne n'est ecarte : un nom ou un genre inconnu se rattrape par un
        # repli (partie locale de l'adresse, masculin par defaut) et se signale
        # a l'utilisateur, sans bloquer l'envoi. Le repli reste visible dans le
        # journal pour qu'il puisse corriger — en ajoutant ",F" sur la ligne
        # pour le genre, ou en editant le nom a la main.
        approximations = []
        if sm.has_name_placeholders(body) or sm.has_gender_placeholders(body):
            for d in dests:
                cle = d.lower()
                if sm.has_name_placeholders(body) and not sm.nom_fiable(d, noms):
                    prenom, nom = sm.resolve_names(d, noms)
                    approximations.append({
                        "addr": d,
                        "raison": f"nom deduit de l'adresse : {nom.upper()}"})
                if sm.has_gender_placeholders(body) and cle not in genres:
                    genres[cle] = "M"
                    approximations.append({
                        "addr": d,
                        "raison": "genre inconnu, masculin par defaut "
                                  "(ajoute \",F\" sur la ligne pour corriger)"})

        if melanger:
            random.shuffle(dests)

        lots = ([dests[i:i + batch] for i in range(0, len(dests), batch)]
                if group else [[d] for d in dests])

        return {
            "sender": sender, "host": host, "port": resolved_port,
            "subject": subject, "subjects": subjects, "body": body,
            "is_html": is_html, "corps_vide": corps_vide, "lots": lots,
            "genres": genres, "noms": noms,
            "approximations": approximations,
            "partagees": [a for a in partagees if a in vues],
            "duree": _duree_secondes(p.get("durationValue"), p.get("durationUnit")),
            "plage": _plage_horaire(p.get("quietStart"), p.get("quietEnd")),
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

    def preflight(self, p):
        """Ce que l'utilisateur doit savoir avant de confirmer un envoi.

        Calcule ici et non dans la page : quotas, cadence et replis sont
        des regles metier, et app.js n'en duplique aucune. La page se contente
        de mettre en forme ce qui est renvoye ici."""
        try:
            cfg = self._prepare(p)
        except (ValueError, sm.SendMailError) as e:
            return {"error": str(e)}

        resume = _resume_campagne(cfg)
        resume.pop("ecarts")   # detail interne, inutile a la page
        return resume

    def cancel(self):
        """Arrete la campagne en cours : elle s'interrompt entre deux messages
        (ou pendant une attente), les deja partis restent partis."""
        self._pause.clear()   # sinon le worker resterait bloque dans _gate()
        self._cancel.set()
        return {"ok": True}

    def pause(self):
        self._pause.set()
        return {"ok": True}

    def resume(self):
        self._pause.clear()
        return {"ok": True}

    def simulate(self, p):
        """Calendrier previsionnel de la campagne, sans toucher au reseau.

        Ne demande que les destinataires : expediteur, objet, corps et mot de
        passe restent facultatifs, et sont simplement rapportes comme manquants
        (`manques`). Verifier un calendrier ne devrait pas exiger d'avoir deja
        redige le message ni sorti ses identifiants.

        La verification d'authentification, elle, vit dans check_auth() : la
        page appelle les deux a la suite, de sorte que le calendrier s'affiche
        immediatement et que l'attente reseau vienne apres."""
        if self._sending:
            return {"error": "une campagne est en cours — arrete-la avant de simuler"}
        try:
            cfg = self._prepare(p, reseau=False)
        except (ValueError, sm.SendMailError) as e:
            return {"error": str(e)}

        # Le calendrier est rejoue avec les memes fonctions que l'envoi reel
        # (planifier + plage horaire), donc sur le nombre de LOTS : en mode
        # groupe un lot part en un seul message et c'est lui qui est cadence.
        resume = _resume_campagne(cfg)
        lots = cfg["lots"]
        instants = horaires_previsionnels(time.time(), resume.pop("ecarts"),
                                          cfg["plage"], len(lots))
        heures, jours = formate_horaires(instants)
        # La derniere ligne porte le jour si la campagne deborde d'aujourd'hui.
        fin = None
        if instants:
            dernier = instants[-1]
            meme_jour = time.localtime(dernier).tm_yday == time.localtime().tm_yday
            fin = heures[-1] if meme_jour else f"{_libelle_jour(dernier)} à {heures[-1]}"

        corps = cfg["body"]
        personnalise = bool(corps) and sm.has_placeholders(corps)
        if personnalise:
            premier = lots[0][0]
            cle = premier.lower()
            corps = sm.personalize(corps, premier, cfg["genres"].get(cle),
                                   cfg["noms"].get(cle))

        manques = []
        if not (p.get("sender") or "").strip():
            manques.append("expéditeur")
        if not cfg["subjects"]:
            manques.append("objet")
        if cfg["corps_vide"]:
            manques.append("corps")
        if not "".join((p.get("password") or "").split()):
            manques.append("mot de passe")

        return dict(resume,
                    lots=[list(lot) for lot in lots],
                    schedule=heures,
                    days=jours,
                    fin=fin,
                    body=corps,
                    personalizedFor=lots[0][0] if personnalise else None,
                    isHtml=cfg["is_html"],
                    manques=manques)

    def check_auth(self, p):
        """Verifie les identifiants SMTP : resolution du serveur, connexion et
        login, puis deconnexion immediate — aucun mail n'est envoye. Separe de
        simulate() pour que le calendrier s'affiche sans attendre le reseau."""
        if self._sending:
            return {"error": "une campagne est en cours"}
        # check_auth et le worker echangent tous deux sys.stderr, qui est
        # global : lancer une verification pendant une campagne ferait
        # restaurer ici le relais du worker, definitivement, et tout ce que la
        # bibliotheque ecrit ensuite disparaitrait du journal.
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
        return {"host": cfg["host"], "port": cfg["port"]}

    def send(self, p):
        """Lance l'envoi dans un thread ; la progression revient par
        onAppEvent (log / error / done)."""
        if self._sending:
            return {"error": "un envoi est deja en cours"}
        try:
            cfg = self._prepare(p, melanger=True)
        except (ValueError, sm.SendMailError) as e:
            return {"error": str(e)}

        pwd = "".join((p.get("password") or "").split())
        if not pwd:
            return {"error": "mot de passe manquant"}

        # Remise a zero avant de lancer le thread, pas dans le finally du
        # worker : cancel() peut legitimement arriver apres l'evenement "done",
        # et une campagne suivante demarrerait alors deja annulee (ou pire,
        # deja en pause, sans rien a l'ecran pour l'expliquer).
        self._cancel.clear()
        self._pause.clear()
        self._sending = True
        threading.Thread(target=self._send_worker, args=(cfg, pwd),
                         daemon=True).start()
        return {"started": True,
                "lots": len(cfg["lots"]),
                "dests": sum(len(lot) for lot in cfg["lots"]),
                "approximations": cfg["approximations"]}

    # ---------------------------------------------------- thread d'envoi

    def _attendre_plage(self, plage):
        """Suspend la campagne hors de la plage horaire autorisee.

        False si l'utilisateur arrete pendant l'attente. Les creneaux sont
        reevalues chaque minute : inutile de calculer la duree exacte jusqu'a
        l'ouverture, et ca reste juste si la machine dort ou change d'heure."""
        prevenu = False
        while not dans_plage(plage):
            if not prevenu:
                debut, fin = plage
                self._emit("log", f"  (hors plage horaire {debut // 60:02d}h-"
                                   f"{fin // 60:02d}h — campagne suspendue)")
                self._emit("progress", {"etat": "hors-plage"})
                prevenu = True
            if not self._wait(60):
                return False
        return self._gate()

    def _send_worker(self, cfg, pwd):
        sender, host, port = cfg["sender"], cfg["host"], cfg["port"]
        relay = _StderrRelay(lambda ligne: self._emit("log", ligne))
        ancien, sys.stderr = sys.stderr, relay
        traites, raison, total = 0, "erreur", len(cfg["lots"])

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

            lots = cfg["lots"]
            ecarts = planifier(cfg)
            ok_total, echecs_total = [], []
            raison = "fini"
            try:
                for i, lot in enumerate(lots):
                    # Hors plage horaire : on attend ici plutot que d'envoyer a
                    # 3 h du matin. La pause manuelle passe par le meme verrou.
                    if not self._attendre_plage(cfg["plage"]):
                        raison = "annule"
                        break

                    if conn[0] is None:
                        conn[0] = connect_fn()
                    debut = time.monotonic()
                    ok, echecs = sm.send_lot(conn, connect_fn, sender, lot,
                                              random.choice(cfg["subjects"]),
                                              cfg["body"], cfg["is_html"],
                                              sm.RETRY_MAX_ATTEMPTS,
                                              cfg["genres"], cfg["noms"],
                                              self._wait)
                    if ok:
                        self._emit("ok", ok)
                    for addr, detail in echecs:
                        self._emit("fail", {"addr": addr,
                                             "detail": sm.fmt_smtp(detail)})
                    ok_total += ok
                    echecs_total += echecs
                    traites = i + 1

                    # L'attente prevue est amputee du temps deja passe a
                    # envoyer (et a reessayer) : sans ca la campagne deborde de
                    # son budget et l'heure de fin annoncee derive des le debut.
                    attente = max(0.0, (ecarts[i] if i < len(ecarts) else 0.0)
                                  - (time.monotonic() - debut))
                    self._emit("progress", {
                        "faits": traites, "total": len(lots),
                        "ok": len(ok_total), "ko": len(echecs_total),
                        "attente": round(attente, 1),
                    })
                    if traites == len(lots):
                        break

                    # Une connexion tenue ouverte pendant des minutes se fait
                    # couper par le serveur et ne ressemble a aucun client
                    # reel, qui se connecte, envoie, puis raccroche.
                    if attente > 60:
                        try:
                            conn[0].quit()
                        except smtplib.SMTPException:
                            pass
                        conn[0] = None
                    if not self._wait(attente):
                        raison = "annule"
                        break
            except sm.SendInterrupted:
                raison = "annule"
            except sm.SendThrottled as e:
                raison = "throttle"
                self._emit("error", f"campagne interrompue : {e}. Reprends plus "
                                     f"tard, en allongeant la duree.")
            except smtplib.SMTPAuthenticationError as e:
                # reconnexion en cours de route refusee
                raison = "erreur"
                self._emit("error", sm.auth_error_msg(host, sender, e))
            except Exception as e:
                # Meme filet de securite pendant l'envoi : send_lot() gere
                # deja les cas SMTP/reseau connus (voir send_mail.py), mais
                # si quelque chose d'imprevu passe au travers, ca doit quand
                # meme finir dans le journal plutot que de tuer le thread en
                # silence — les lots restants du batch sont alors abandonnes.
                raison = "erreur"
                self._emit("error", f"erreur inattendue pendant l'envoi : {e!r}")
            finally:
                try:
                    if conn[0] is not None:
                        conn[0].quit()
                except smtplib.SMTPException:
                    pass

            self._emit("summary", {"ok": len(ok_total), "ko": len(echecs_total)})
        finally:
            sys.stderr = ancien
            self._sending = False
            # "restants" n'est pas un echec : ces destinataires n'ont jamais
            # ete tentes. Le resume {ok, ko} seul ne sait pas dire la
            # difference, alors qu'apres un arret c'est le gros du fichier.
            self._emit("done", {"raison": raison,
                                 "restants": max(total - traites, 0)})


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

    # Fermer la fenetre doit arreter la campagne. Le thread est daemon, donc
    # l'interpreteur finirait par le tuer, mais sans garantie de moment : une
    # campagne etalee sur des heures continuerait d'envoyer alors que
    # l'utilisateur croit avoir tout arrete. _pause est relache d'abord, sinon
    # le worker resterait bloque dans _gate() au lieu de se derouler.
    def _arreter_a_la_fermeture():
        api._pause.clear()
        api._cancel.set()
        return True   # ne bloque pas la fermeture

    window.events.closing += _arreter_a_la_fermeture

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
