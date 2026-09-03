"""Logique metier de l'envoi de mail (SMTP, retry, personnalisation...).

Module bibliotheque uniquement — pas de CLI, pas de point d'entree. Toute
l'interface utilisateur vit dans app.py (GUI pywebview), qui importe ce
module et appelle ses fonctions directement.

Format : texte ou HTML (voir build()). En HTML, un repli texte brut (tags
retires) est genere via html2text et joint pour les clients qui ne rendent
pas le HTML.

Serveur SMTP : resolve_smtp() le detecte automatiquement a partir du domaine
de l'expediteur — tente smtp.<domaine>, puis mail.<domaine>, puis
smtp.mail.<domaine> (connexion reelle pour verifier qu'un serveur repond).
PROVIDERS ne liste que les exceptions dont le vrai serveur ne suit pas ce
schema (ex. outlook.com -> smtp.office365.com).

Envoi par lots : send_lot() gere le retry/backoff sur les erreurs 4xx et la
redecoupe automatique d'un lot trop grand pour le serveur (452 sur tout le
lot). Un lot de taille 1 est le cas par defaut (un mail par destinataire) ;
un lot plus grand correspond a un envoi groupe (tous en To).

Personnalisation : {{FIRST}}/{{LAST}} dans le corps sont remplaces par
destinataire (voir personalize()/split_name()), a partir de la partie locale
(avant @) de son adresse. Avec un '.' : FIRST = avant le premier point, LAST
= tout ce qui suit. Sans '.' : FIRST vide, LAST = toute la partie locale.
N'a de sens que pour un lot de taille 1 (voir send_lot()).

Extraction d'adresses : read_list()/scan_addresses() tolerent du texte libre
(CSV, "Nom <adresse>", copier-coller de tableur) — voir leur docstring.
"""
import json
import random
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import html2text

ADDR_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


class SendMailError(Exception):
    """Erreur de validation/configuration (domaine SMTP, fichier d'adresses...).
    Levee par les fonctions reutilisables (resolve_smtp, read_list) plutot que
    sys.exit, pour rester appelables depuis un appelant non-CLI (ex. une GUI)."""

# Exceptions dont le vrai serveur SMTP ne correspond a aucun des schemas
# devines par resolve_smtp (smtp./mail./smtp.mail.<domaine>). Domaine (en
# minuscules) -> (hote SMTP, port) ; --smtp-host/--smtp-port passent outre.
PROVIDERS = {
    "googlemail.com": ("smtp.gmail.com", 587),
    "outlook.com": ("smtp.office365.com", 587),
    "outlook.fr": ("smtp.office365.com", 587),
    "hotmail.com": ("smtp.office365.com", 587),
    "hotmail.fr": ("smtp.office365.com", 587),
    "live.com": ("smtp.office365.com", 587),
    "msn.com": ("smtp.office365.com", 587),
    "yahoo.fr": ("smtp.mail.yahoo.com", 587),
    "icloud.com": ("smtp.mail.me.com", 587),
    "mac.com": ("smtp.mail.me.com", 587),
    "gmx.fr": ("mail.gmx.com", 587),
}

# Domaines connus qui n'offrent pas de SMTP standard sans passerelle a part.
NO_SMTP = {
    "protonmail.com": "Proton necessite Proton Mail Bridge (payant) pour le SMTP classique.",
    "proton.me": "Proton necessite Proton Mail Bridge (payant) pour le SMTP classique.",
    "pm.me": "Proton necessite Proton Mail Bridge (payant) pour le SMTP classique.",
}

# Backoff exponentiel sur les erreurs 4xx (temporaires par definition RFC 5321,
# throttling/quota/surcharge serveur). Bornes basses volontairement : ce script
# est un envoi synchrone en premier plan, pas une file d'attente de MTA — les
# vraies politiques des providers (Gmail, Outlook...) recommandent des delais
# de plusieurs minutes a plusieurs heures entre tentatives, inadapte ici. Pour
# un throttling persistant, relance le script plus tard plutot que d'allonger
# ces constantes.
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 2.0    # secondes, double a chaque tentative
RETRY_MAX_DELAY = 30.0    # plafond par attente


def fmt_smtp(detail):
    """Formate un (code, message) smtplib pour l'affichage."""
    code, msg = detail
    if isinstance(msg, bytes):
        msg = msg.decode(errors="replace")
    return f"{code} {msg}" if code else str(msg)


def auth_error_msg(host, sender, e):
    """Message clair pour un 535/SMTPAuthenticationError (mot de passe refuse)."""
    detail = fmt_smtp((getattr(e, "smtp_code", None), getattr(e, "smtp_error", str(e))))
    return (
        f"authentification refusee par {host} pour {sender} : {detail}\n"
        f"  -> verifie : mot de passe d'application (pas le mot de passe du "
        f"compte), sans espace residuel, genere pour CE compte precis, "
        f"validation en 2 etapes active. Si le compte a la Protection avancee "
        f"activee, les mots de passe d'application sont bloques par conception "
        f"(passer par OAuth2/API dans ce cas)."
    )


def probe_smtp(host, port, timeout=6):
    """Verifie qu'un serveur SMTP repond bien sur host:port."""
    try:
        with smtplib.SMTP(host, port, timeout=timeout):
            pass
        return True
    except (OSError, smtplib.SMTPException):
        return False


def resolve_smtp(email, host_override, port_override):
    domain = email.rsplit("@", 1)[-1].lower()
    if host_override:
        return host_override, port_override or 587
    if domain in NO_SMTP:
        raise SendMailError(f"{domain} : {NO_SMTP[domain]} (utilise --smtp-host pour forcer)")
    if domain in PROVIDERS:
        host, port = PROVIDERS[domain]
        return host, port_override or port

    port = port_override or 587
    candidats = [f"smtp.{domain}", f"mail.{domain}", f"smtp.mail.{domain}"]
    for host in candidats:
        print(f"  (detection SMTP : test de {host}:{port}...)", file=sys.stderr)
        if probe_smtp(host, port):
            print(f"  (detection SMTP : {host}:{port} repond, utilise)", file=sys.stderr)
            return host, port
    raise SendMailError(
        f"domaine {domain!r} non reconnu et aucun serveur ne repond parmi "
        f"{', '.join(candidats)} — precise --smtp-host (et --smtp-port si besoin)")


# Extraction d'adresses depuis du texte libre (CSV, copier-coller de tableur,
# "Nom <adresse>", "mailto:...", notes en fin de ligne...). Volontairement plus
# stricte que la grammaire RFC 5322, dont les formes exotiques n'apparaissent
# jamais dans un export et ne produiraient que des faux positifs :
#   - partie locale : demarre par un alphanumerique (pas de point en tete, ce
#     qui evite d'avaler la ponctuation qui precede) ;
#   - domaine : au moins un point et un TLD alphabetique d'au moins 2
#     caracteres, ce qui ecarte "user@localhost" et les numeros de version.
# Le TLD strictement alphabetique fait aussi que la ponctuation finale d'une
# phrase ("ecris a jean@example.com.") n'est pas capturee.
EMAIL_RE = re.compile(
    r"[A-Za-z0-9_%+-][A-Za-z0-9._%+-]*"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)


def read_text_tolerant(path):
    """Lit un fichier texte en tolerant les encodages courants des exports.
    Excel produit de l'utf-8 avec BOM ou du cp1252 selon les versions ;
    latin-1 en dernier recours ne peut jamais echouer, quitte a mal rendre un
    accent — mieux vaut un nom approximatif qu'un fichier illisible, les
    adresses elles-memes etant en ASCII."""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")  # inatteignable en pratique


def _scan_json(text):
    """Parcourt un JSON valide et renvoie les adresses trouvees dans toutes
    les valeurs chaine, a n'importe quelle profondeur (deduplique, ordre de
    premiere apparition), ou None si text n'est pas du JSON valide.

    Contrairement au mode ligne (une seule adresse retenue par ligne, pour
    eviter d'avaler une colonne CSV non voulue), on garde ICI toutes les
    adresses trouvees : un objet JSON n'a pas de notion de "colonne
    ambigue", et un export d'API est typiquement minifie sur une poignee de
    lignes voire une seule — s'arreter a la premiere adresse de "la ligne"
    perdrait alors la quasi-totalite des entrees."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    vus = set()
    trouvees = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for addr in EMAIL_RE.findall(node):
                if addr not in vus:
                    vus.add(addr)
                    trouvees.append(addr)

    walk(data)
    return trouvees


def scan_addresses(text):
    """Extrait les adresses d'un texte.

    Deux modes, choisis en sniffant le contenu (pas l'extension du fichier,
    pour rester correct meme si un JSON est renomme en .txt) :

    - JSON valide (le texte commence par '{' ou '[') : parcours recursif de
      toutes les valeurs chaine, voir _scan_json(). Robuste face a un export
      minifie sur une seule ligne, ce que le mode ligne ne serait pas.
    - Sinon, mode ligne : une adresse par ligne, mais la ligne peut contenir
      n'importe quoi d'autre (colonnes CSV, nom, telephone). Seule la
      PREMIERE adresse de chaque ligne est retenue : un export avec une
      colonne 'email_manager' ne doit pas ajouter de destinataire a l'insu
      de l'utilisateur. Les lignes sans adresse (en-tetes CSV, separateurs,
      commentaires) sont ignorees au lieu d'etre fatales — c'est ce qui
      permet d'avaler un CSV brut.

    Renvoie (adresses, stats) ; stats sert a expliquer ce qui a ete ecarte
    (vide en mode JSON, ce decompte par ligne n'y a pas de sens).
    """
    if text.lstrip()[:1] in ("{", "["):
        trouvees = _scan_json(text)
        if trouvees is not None:
            return trouvees, {"ignorees": 0, "multiples": 0, "json": True}
        # ressemblait a du JSON mais invalide (tronque, corrompu) -> repli

    adresses = []
    ignorees = 0
    multiples = 0
    for ligne in text.splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        if not ligne:
            continue
        trouvees = EMAIL_RE.findall(ligne)
        if not trouvees:
            ignorees += 1
            continue
        if len(trouvees) > 1:
            multiples += 1
        adresses.append(trouvees[0])
    return adresses, {"ignorees": ignorees, "multiples": multiples, "json": False}


def read_list(path):
    """Adresses d'un fichier (voir scan_addresses pour les regles)."""
    adresses, _ = scan_addresses(read_text_tolerant(path))
    if not adresses:
        raise SendMailError(f"{path} : aucune adresse email trouvee")
    return adresses


def html_to_text(body):
    """Repli texte brut pour les clients qui ne rendent pas le HTML."""
    conv = html2text.HTML2Text()
    conv.body_width = 0  # pas de retour a la ligne force
    return conv.handle(body).strip()


PLACEHOLDERS = ("{{FIRST}}", "{{LAST}}", "{{TITLE}}", "{{TERM}}")

# Sous-ensemble qui exige de connaitre le genre du destinataire. Rien dans
# une adresse email ne permet de le deviner : il doit venir d'une source
# externe (voir l'argument `genre` de personalize()).
GENDER_PLACEHOLDERS = ("{{TITLE}}", "{{TERM}}")

TITRES = {"M": "M.", "F": "Mme."}
TERMINAISONS = {"M": "", "F": "e"}


def has_placeholders(body):
    return any(p in body for p in PLACEHOLDERS)


def has_gender_placeholders(body):
    return any(p in body for p in GENDER_PLACEHOLDERS)


def split_name(addr):
    """Derive (FIRST, LAST) a partir de la partie locale (avant @) de addr.
    Avec un '.' : FIRST = avant le premier point, LAST = tout ce qui suit
    (points suivants inclus). Sans '.' : FIRST vide, LAST = toute la partie
    locale. Casse d'origine preservee, pas de capitalisation automatique."""
    local = addr.split("@", 1)[0]
    if "." in local:
        first, _, last = local.partition(".")
    else:
        first, last = "", local
    return first, last


def personalize(body, addr, genre=None):
    """Remplace les placeholders dans body pour le destinataire addr.

    {{FIRST}}/{{LAST}} viennent de l'adresse (voir split_name).
    {{TITLE}} -> "M."/"Mme." et {{TERM}} -> ""/"e" (accord grammatical :
    "inscrit{{TERM}}") demandent `genre` valant "M" ou "F".

    Genre inconnu (None) : les deux deviennent des chaines vides. Rien ne
    peut etre devine depuis une adresse seule, et laisser "{{TITLE}}" visible
    dans le mail expedie serait pire qu'un blanc — l'appelant est prevenu en
    amont (voir has_gender_placeholders)."""
    first, last = split_name(addr)
    titre = TITRES.get(genre, "") if genre else ""
    terminaison = TERMINAISONS.get(genre, "") if genre else ""
    return (body
            .replace("{{FIRST}}", first)
            .replace("{{LAST}}", last)
            .replace("{{TITLE}}", titre)
            .replace("{{TERM}}", terminaison))


def build(sender, to, subject, body, is_html=False):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if is_html:
        msg.set_content(html_to_text(body) or " ")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg


def connect(host, port, sender, pwd):
    s = smtplib.SMTP(host, port, timeout=30)
    s.starttls(context=ssl.create_default_context())
    s.login(sender, pwd)
    return s


def send_lot(conn, connect_fn, sender, lot, subject, body, is_html, max_retries,
             genres=None):
    """Envoie un mail a `lot` en gerant les erreurs SMTP transitoires.

    `conn` est une liste a 1 element portant la connexion active (mutable,
    pour pouvoir la remplacer sur reconnexion). Gere :
      - 452 sur tout le lot (SMTPRecipientsRefused) : lot trop grand pour ce
        serveur (limite RCPT TO) -> redecoupe en deux et reessaie chaque
        moitie immediatement, sans attente (c'est structurel, pas temporaire) ;
      - refus partiel (certaines adresses seulement, pas d'exception levee) :
        rapporte comme echec definitif par adresse, pas de retry (une adresse
        invalide/pleine le reste) ;
      - 4xx generique (throttling, surcharge serveur au niveau DATA/MAIL) :
        backoff exponentiel borne, jusqu'a max_retries tentatives ;
      - erreur reseau (OSError : hote injoignable, connexion refusee/reset,
        timeout) : meme traitement que le 4xx generique ci-dessus ;
      - connexion perdue en cours de route : reconnexion puis reessai ;
      - 5xx ou tentatives epuisees : echec definitif.

    Si lot est un destinataire unique, les placeholders de body sont
    personnalises pour cette adresse avant l'envoi (voir personalize()).
    `genres` est un dict {adresse en minuscules: "M"/"F"} pour {{TITLE}} et
    {{TERM}} ; une adresse absente donne un genre inconnu, pas une erreur.

    Renvoie (ok: liste d'adresses envoyees, echecs: liste de (adresse, detail)).
    """
    if len(lot) == 1:
        body = personalize(body, lot[0], (genres or {}).get(lot[0].lower()))
    delay = RETRY_BASE_DELAY
    for attempt in range(1, max_retries + 1):
        try:
            refused = conn[0].send_message(build(sender, lot, subject, body, is_html))
            ok = [a for a in lot if a not in refused]
            echecs = [(a, refused[a]) for a in refused]
            return ok, echecs
        except smtplib.SMTPServerDisconnected:
            print("  (connexion perdue, reconnexion...)", file=sys.stderr)
            conn[0] = connect_fn()
            continue
        except smtplib.SMTPRecipientsRefused as e:
            if len(lot) > 1 and any(code == 452 for code, _ in e.recipients.values()):
                mid = len(lot) // 2
                print(f"  (452 sur le lot de {len(lot)} — trop de destinataires "
                      f"pour ce serveur, redecoupe en {mid}+{len(lot) - mid})",
                      file=sys.stderr)
                ok1, ko1 = send_lot(conn, connect_fn, sender, lot[:mid], subject,
                                     body, is_html, max_retries, genres)
                ok2, ko2 = send_lot(conn, connect_fn, sender, lot[mid:], subject,
                                     body, is_html, max_retries, genres)
                return ok1 + ok2, ko1 + ko2
            return [], list(e.recipients.items())
        except (smtplib.SMTPResponseException, smtplib.SMTPSenderRefused) as e:
            code = getattr(e, "smtp_code", None)
            transitoire = code is not None and 400 <= code < 500
            if not transitoire or attempt == max_retries:
                detail = (code, getattr(e, "smtp_error", str(e)))
                return [], [(a, detail) for a in lot]
            wait = min(delay, RETRY_MAX_DELAY) + random.uniform(0, 1)
            print(f"  (erreur {code} temporaire, tentative {attempt}/{max_retries}, "
                  f"nouvel essai dans {wait:.0f}s...)", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
        except OSError as e:
            # Destinataire/serveur injoignable au niveau reseau (connexion
            # refusee, hote injoignable, timeout, coupure en plein DATA) :
            # pas une exception smtplib, mais une vraie cause d'echec, pas
            # question de la laisser remonter en silence. Traite comme
            # transitoire (retry) : un blip reseau se resout souvent tout
            # seul, contrairement a un rejet SMTP explicite.
            if attempt == max_retries:
                return [], [(a, (None, f"erreur reseau : {e}")) for a in lot]
            wait = min(delay, RETRY_MAX_DELAY) + random.uniform(0, 1)
            print(f"  (erreur reseau ({e}), tentative {attempt}/{max_retries}, "
                  f"nouvel essai dans {wait:.0f}s...)", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
    return [], [(a, (None, "tentatives epuisees")) for a in lot]


if __name__ == "__main__":
    sys.exit("send_mail.py est une bibliotheque, pas un script — lance "
              "'python app.py' pour l'interface graphique.")
