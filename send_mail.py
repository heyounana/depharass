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
destinataire (voir personalize()). Les noms viennent d'une table fournie par
l'appelant — le CSV des deputes, qui fait autorite ; a defaut seulement,
derive_names() tente une deduction depuis la partie locale de l'adresse, et
ne rend rien des qu'il y a le moindre doute. Une adresse sans nom fiable doit
etre ecartee en amont plutot que personnalisee a vide : "Bonjour  CONTACT,"
est la signature meme d'un envoi sur liste. N'a de sens que pour un lot de
taille 1 (voir send_lot()).

Cadence : gaps() repartit N envois sur une duree donnee avec des ecarts
aleatoires bornes, cout d'envoi deduit. C'est l'appelant qui attend entre deux
lots — send_lot() ne connait que ses propres reessais.

Extraction d'adresses : read_list()/scan_addresses() tolerent du texte libre
(CSV, "Nom <adresse>", copier-coller de tableur) — voir leur docstring.
"""
import json
import random
import re
import secrets
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import html2text

# Identification de l'emetteur dans le header User-Agent. Honnete : se faire
# passer pour Thunderbird ne tromperait aucun filtre serieux (ils pesent le
# comportement, pas cette chaine) et serait une fausse declaration.
USER_AGENT = "depharass/1.0"

# Validation stricte d'une adresse saisie telle quelle (De, une ligne de
# Destinataires) : uniquement les caracteres effectivement valides dans une
# adresse email courante — pas "tout sauf @ et espace", qui laissait passer
# "jean,paul@x.com" ou "<script>@x.com". Meme famille de caracteres que
# EMAIL_RE plus bas, mais ancree sur toute la chaine (ici on valide un champ
# complet, EMAIL_RE cherche une adresse au milieu d'autre chose), et chaque
# etiquette de domaine doit commencer/finir par un alphanumerique — un tiret
# en tete/queue n'est pas une etiquette DNS valide ("-x.com", "x-.com").
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
ADDR_RE = re.compile(
    rf"^[A-Za-z0-9_%+-][A-Za-z0-9._%+-]*"
    rf"@(?:{_DOMAIN_LABEL}\.)+[A-Za-z]{{2,}}$"
)


class SendMailError(Exception):
    """Erreur de validation/configuration (domaine SMTP, fichier d'adresses...).
    Levee par les fonctions reutilisables (resolve_smtp, read_list) plutot que
    sys.exit, pour rester appelables depuis un appelant non-CLI (ex. une GUI)."""


class SendInterrupted(SendMailError):
    """L'appelant a demande l'arret pendant une attente (voir l'argument
    `attendre` de send_lot). Les destinataires restants n'ont pas ete tentes,
    ce qui n'est pas la meme chose qu'un echec."""


class SendThrottled(SendMailError):
    """Le serveur demande de ralentir de facon repetee. Insister message par
    message ne ferait qu'aggraver le probleme : c'est a l'appelant d'arreter
    la campagne et de la reprendre plus tard."""

# Exceptions dont le vrai serveur SMTP ne correspond a aucun des schemas
# devines par resolve_smtp (smtp./mail./smtp.mail.<domaine>). Domaine (en
# minuscules) -> (hote SMTP, port) ; --smtp-host/--smtp-port passent outre.
PROVIDERS = {
    # gmail.com suit pourtant le schema smtp.<domaine>, mais l'y laisser
    # deviner coutait une connexion de test a chaque simulation/envoi pour le
    # domaine expediteur le plus courant.
    "gmail.com": ("smtp.gmail.com", 587),
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
# throttling/quota/surcharge serveur). Delais volontairement longs : un 4xx
# veut le plus souvent dire "tu vas trop vite", et reessayer 2 s plus tard est
# exactement l'inverse de ce qui est demande — c'est une des causes probables
# des comptes bloques. Les campagnes etant desormais etalees dans le temps
# (voir gaps() et le worker de app.py), rien n'impose plus des bornes basses.
RETRY_MAX_ATTEMPTS = 4
RETRY_BASE_DELAY = 60.0    # secondes, double a chaque tentative
RETRY_MAX_DELAY = 900.0    # plafond par attente (15 min)

# Codes et formulations qui signifient "ralentis", par opposition a une panne
# ponctuelle. Y insister n'aide pas : au bout de THROTTLE_ABANDON refus
# consecutifs, l'appelant est cense arreter la campagne (voir SendThrottled).
THROTTLE_CODES = {421, 450, 451, 452}
THROTTLE_INDICES = ("rate", "limit", "throttl", "too many", "quota",
                    "try again", "slow down", "deferred", "unusual")
THROTTLE_ABANDON = 3


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
        f"compte), genere pour CE compte precis, "
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

# Titre par ligne, mode CSV uniquement (pas de sens sur du JSON). Un champ
# valant exactement M, H ou F (H = homme, accepte comme synonyme de M),
# borne par des separateurs virgule/point-virgule ou le debut/fin de ligne —
# jamais une lettre isolee ailleurs dans le texte, pour eviter les faux
# positifs (une initiale, un gabarit "M"/"L"/"XL"...). Guillemets et espaces
# autour du champ tolerees.
TITLE_RE = re.compile(r'(?:^|[,;])\s*"?([MHFmhf])"?\s*(?=[,;]|$)')

# Lettre saisie -> genre interne. H (homme) est un synonyme de M ; la casse
# est libre partout ou un titre est accepte.
TITRE_LETTRES = {"M": "M", "H": "M", "F": "F"}


def _detect_title(ligne):
    m = TITLE_RE.search(ligne)
    if not m:
        return None
    return TITRE_LETTRES[m.group(1).upper()]


def split_recipient(ligne):
    """Decoupe une ligne du champ Destinataires : "adresse" ou "adresse,T",
    ou T vaut H, M ou F (casse libre, espaces toleres autour).

    Renvoie (adresse, "M"/"F"/None). L'adresse n'est pas validee ici, c'est
    le role d'ADDR_RE chez l'appelant. Une lettre presente mais inconnue
    leve une erreur plutot que d'etre ignoree : une faute de frappe doit se
    voir, sinon le mail partirait sans titre sans que personne le remarque."""
    adresse, separateur, reste = ligne.partition(",")
    adresse = adresse.strip()
    if not separateur:
        return adresse, None
    lettre = reste.strip()
    genre = TITRE_LETTRES.get(lettre.upper())
    if genre is None:
        raise SendMailError(
            f"titre inconnu apres la virgule : {lettre!r} (attendu H, M ou F) "
            f"— ligne : {ligne.strip()!r}")
    return adresse, genre


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
    """Extrait les adresses d'un texte, avec titre eventuel par ligne.

    Deux modes, choisis en sniffant le contenu (pas l'extension du fichier,
    pour rester correct meme si un JSON est renomme en .txt) :

    - JSON valide (le texte commence par '{' ou '[') : parcours recursif de
      toutes les valeurs chaine, voir _scan_json(). Robuste face a un export
      minifie sur une seule ligne, ce que le mode ligne ne serait pas. Pas de
      detection de titre ici (pas de notion de "champ CSV" dans du JSON).
    - Sinon, mode ligne : une adresse par ligne, mais la ligne peut contenir
      n'importe quoi d'autre (colonnes CSV, nom, telephone). Seule la
      PREMIERE adresse de chaque ligne est retenue : un export avec une
      colonne 'email_manager' ne doit pas ajouter de destinataire a l'insu
      de l'utilisateur. Les lignes sans adresse (en-tetes CSV, separateurs,
      commentaires) sont ignorees au lieu d'etre fatales — c'est ce qui
      permet d'avaler un CSV brut. Si la meme ligne porte un champ M/H/F
      (voir _detect_title), il est associe a l'adresse trouvee.

    Renvoie (adresses, titres, stats) : titres est {adresse en minuscules:
    "M"/"F"} pour les seules lignes ou un titre a ete detecte ; stats sert a
    expliquer ce qui a ete ecarte (vide en mode JSON, ce decompte par ligne
    n'y a pas de sens).
    """
    if text.lstrip()[:1] in ("{", "["):
        trouvees = _scan_json(text)
        if trouvees is not None:
            return trouvees, {}, {"ignorees": 0, "multiples": 0, "json": True}
        # ressemblait a du JSON mais invalide (tronque, corrompu) -> repli

    adresses = []
    titres = {}
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
        adresse = trouvees[0]
        adresses.append(adresse)
        titre = _detect_title(ligne)
        if titre:
            titres[adresse.lower()] = titre
    return adresses, titres, {"ignorees": ignorees, "multiples": multiples, "json": False}


def read_list(path):
    """Adresses d'un fichier (voir scan_addresses pour les regles)."""
    adresses, _, _ = scan_addresses(read_text_tolerant(path))
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

# Sous-ensemble qui exige un nom identifiable (voir resolve_names()).
NAME_PLACEHOLDERS = ("{{FIRST}}", "{{LAST}}")

TITRES = {"M": "M.", "F": "Mme."}
TERMINAISONS = {"M": "", "F": "e"}

# Boites generiques : jamais le nom d'une personne. Une telle adresse ne doit
# pas produire "Bonjour  CONTACT" — elle n'est simplement pas personnalisable.
BOITES_GENERIQUES = {
    "contact", "info", "infos", "secretariat", "accueil", "presse", "cabinet",
    "permanence", "mairie", "depute", "deputee", "assemblee", "circonscription",
    "admin", "webmaster", "postmaster", "noreply", "no-reply", "bureau",
    "assistant", "assistante", "communication", "courrier",
}

# Un morceau de nom : au moins deux lettres (accents compris), separees au
# besoin par un trait d'union ou une apostrophe ("anne-marie", "o'brien").
# Ecarte les chiffres, les codes, et les initiales seules — "j.dupont" ne doit
# pas donner "Bonjour J DUPONT".
_MORCEAU_NOM = re.compile(r"^[^\W\d_](?:['’-]?[^\W\d_])+$")


def _capitaliser(morceau):
    """Capitalise chaque partie d'un nom compose : anne-marie -> Anne-Marie."""
    out = morceau
    for sep in ("-", "'", "’"):
        out = sep.join(p[:1].upper() + p[1:] for p in out.split(sep))
    return out


def has_placeholders(body):
    return any(p in body for p in PLACEHOLDERS)


def has_gender_placeholders(body):
    return any(p in body for p in GENDER_PLACEHOLDERS)


def has_name_placeholders(body):
    return any(p in body for p in NAME_PLACEHOLDERS)


def derive_names(addr):
    """(prenom, nom) devines depuis la partie locale, ou (None, None).

    Ne devine que sur un motif "prenom.nom" franc : tout le reste — boite
    generique, mot unique, plus de deux morceaux, chiffres — ne renvoie rien.
    Mieux vaut ne pas personnaliser que d'ecrire "Bonjour  CONTACT", qui est
    la signature meme d'un envoi sur liste.

    Ce qu'on devine ici reste une approximation : "marine.lepen" donne "Lepen"
    et non "Le Pen", les particules et noms composes sont perdus. C'est le
    repli pour une adresse saisie a la main ; les deputes passent par le CSV,
    qui fait autorite (voir resolve_names)."""
    local = addr.split("@", 1)[0].lower()
    prenom, _, nom = local.partition(".")
    if not nom or local in BOITES_GENERIQUES:
        return None, None
    if prenom in BOITES_GENERIQUES or nom in BOITES_GENERIQUES:
        return None, None
    if not (_MORCEAU_NOM.match(prenom) and _MORCEAU_NOM.match(nom)):
        return None, None
    return _capitaliser(prenom), nom


def resolve_names(addr, table=None):
    """(prenom, nom) pour addr, ou (None, None) si rien de fiable.

    `table` — {adresse en minuscules: (prenom, nom)} — fait autorite : c'est
    le CSV des deputes, ou les noms sont exacts. Une adresse absente retombe
    sur la deduction depuis l'adresse elle-meme."""
    depuis_table = (table or {}).get(addr.lower())
    if depuis_table:
        return depuis_table
    return derive_names(addr)


def personalize(body, addr, genre=None, names=None):
    """Remplace les placeholders dans body pour le destinataire addr.

    `names` — le couple (prenom, nom) deja resolu par l'appelant, qui a acces
    au CSV. A defaut, resolve_names() se debrouille avec la seule adresse.
    {{LAST}} part en majuscules (convention "Prenom NOM") ; resolve_names()
    garde la casse d'origine, c'est un choix de presentation propre a
    personalize().

    {{TITLE}} -> "M."/"Mme." et {{TERM}} -> ""/"e" (accord grammatical :
    "inscrit{{TERM}}") demandent `genre` valant "M" ou "F".

    Genre ou nom inconnu : les placeholders concernes deviennent des chaines
    vides. C'est un filet, pas un mode de fonctionnement — l'appelant est
    cense avoir ecarte ces destinataires en amont (voir has_name_placeholders
    et has_gender_placeholders), justement pour ne jamais expedier
    "Bonjour  ,"."""
    prenom, nom = names if names else resolve_names(addr)
    titre = TITRES.get(genre, "") if genre else ""
    terminaison = TERMINAISONS.get(genre, "") if genre else ""
    return (body
            .replace("{{FIRST}}", prenom or "")
            .replace("{{LAST}}", (nom or "").upper())
            .replace("{{TITLE}}", titre)
            .replace("{{TERM}}", terminaison))


# Enveloppe HTML minimale. Quill ne produit qu'une soupe de <p> ; un vrai
# client mail envoie toujours un document complet avec un <head>.
HTML_DOC = ('<!DOCTYPE html>\n<html>\n<head>\n'
            '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n'
            '</head>\n<body>\n{corps}\n</body>\n</html>')


def _boundary():
    """Frontiere MIME de la forme qu'emettent les clients courants. La stdlib
    genere sinon "===============<19 chiffres>==", signature Python
    reconnaissable au premier coup d'oeil."""
    return "------------" + secrets.token_hex(12)


def _message_id(sender):
    """Message-ID sur le domaine de l'expediteur.

    email.utils.make_msgid() est ecarte volontairement : il divulgue le nom
    d'hote de la machine (<...@mon-portable>) et sa forme
    <horodatage.pid.aleatoire@...> est une empreinte Python."""
    domaine = sender.rsplit("@", 1)[-1]
    return f"<{secrets.token_hex(16)}@{domaine}>"


def build(sender, to, subject, body, is_html=False):
    """Construit le message, headers poses explicitement dans l'ordre ou un
    client mail les emet.

    Date et Message-ID etaient absents : Date est pourtant obligatoire
    (RFC 5322), et leur absence a la soumission est anormale en soi — Gmail
    les rajoute silencieusement, la plupart des autres serveurs non."""
    msg = EmailMessage()
    msg["Message-ID"] = _message_id(sender)
    msg["Date"] = formatdate(localtime=True)
    msg["MIME-Version"] = "1.0"
    msg["User-Agent"] = USER_AGENT
    msg["To"] = ", ".join(to)
    msg["From"] = sender
    msg["Subject"] = subject

    # cte explicite : sans lui Python encode en base64 des qu'il y a un accent.
    # Un corps de prose integralement en base64 ne ressemble a aucun client
    # grand public — et c'etait le cas de tous les mails francais partis d'ici.
    if is_html:
        msg.set_content(html_to_text(body) or " ", cte="quoted-printable")
        msg.add_alternative(HTML_DOC.format(corps=body), subtype="html",
                            cte="quoted-printable")
        msg.set_boundary(_boundary())
        # add_alternative() glisse un MIME-Version dans la sous-partie HTML :
        # ce header n'a de sens qu'en tete du message, jamais dans une partie.
        for partie in msg.iter_parts():
            del partie["MIME-Version"]
    else:
        msg.set_content(body, cte="quoted-printable")
    return msg


def connect(host, port, sender, pwd):
    s = smtplib.SMTP(host, port, timeout=30)
    s.starttls(context=ssl.create_default_context())
    s.login(sender, pwd)
    return s


# Cout moyen d'un envoi (TCP + TLS + AUTH + DATA + QUIT), retranche du budget :
# sans ca une campagne de 577 messages deborde d'une demi-heure et l'heure de
# fin annoncee est fausse des la premiere minute.
COUT_ENVOI = 3.0

# Bornes d'un ecart, en multiples de l'ecart moyen.
ECART_MIN, ECART_MAX = 0.2, 3.0


def gaps(n, duree, cout_envoi=COUT_ENVOI):
    """Ecarts en secondes entre `n` envois etales sur `duree`.

    Renvoie n-1 valeurs : on n'attend pas apres le dernier message.

    Tirage exponentiel plutot que regulier — un intervalle constant au
    metronome est en soi un marqueur. Les valeurs sont bornees autour de la
    moyenne, puis l'ecart au budget est reparti sur celles qui ont encore de
    la marge : renormaliser tout le vecteur d'un coup ressortirait des valeurs
    hors bornes."""
    k = max(n - 1, 0)
    if k == 0:
        return []
    budget = duree - n * cout_envoi
    if budget <= 0:
        # Duree plus courte que le temps d'envoi lui-meme. On etale quand meme
        # sur la duree brute plutot que de renvoyer des ecarts nuls : la
        # campagne debordera, mais rendre le champ silencieusement inoperant
        # ramenerait au tir en rafale qu'on cherche justement a eviter. Le
        # preflight, lui, previent que la cadence est trop rapide.
        budget = duree
    moyenne = budget / k
    if moyenne <= 0:
        return [0.0] * k

    bas, haut = ECART_MIN * moyenne, ECART_MAX * moyenne
    ecarts = [min(max(random.expovariate(1 / moyenne), bas), haut)
              for _ in range(k)]
    for _ in range(20):
        residu = budget - sum(ecarts)
        if abs(residu) < 0.5:
            break
        souples = [i for i, g in enumerate(ecarts)
                   if (residu > 0 and g < haut) or (residu < 0 and g > bas)]
        if not souples:
            break
        part = residu / len(souples)
        for i in souples:
            ecarts[i] = min(max(ecarts[i] + part, bas), haut)
    return ecarts


def est_throttling(code, message):
    """Le serveur demande-t-il de ralentir, par opposition a une panne ?"""
    if code in THROTTLE_CODES:
        return True
    texte = (message.decode(errors="replace") if isinstance(message, bytes)
             else str(message)).lower()
    return any(indice in texte for indice in THROTTLE_INDICES)


def send_lot(conn, connect_fn, sender, lot, subject, body, is_html, max_retries,
             genres=None, noms=None, attendre=None):
    """Envoie un mail a `lot` en gerant les erreurs SMTP transitoires.

    `conn` est une liste a 1 element portant la connexion active (mutable,
    pour pouvoir la remplacer sur reconnexion).

    `attendre(secondes) -> bool` dit *comment* patienter, et renvoie False si
    l'appelant veut arreter — ce qui leve SendInterrupted. Par defaut
    time.sleep, donc rien ne change pour un appelant que ca n'interesse pas.
    C'est ce qui permet a la GUI d'interrompre une campagne pendant une attente
    de 15 min sans importer ici la moindre notion de thread ou de fenetre.

    Gere :
      - 452 sur tout le lot : lot trop grand pour ce serveur (limite RCPT TO)
        -> redecoupe en deux, apres une pause ;
      - throttling (voir est_throttling) : backoff long, puis SendThrottled —
        insister message par message ne fait qu'aggraver, c'est a l'appelant
        d'arreter la campagne ;
      - refus partiel (certaines adresses seulement, pas d'exception levee) :
        rapporte comme echec definitif par adresse, pas de retry (une adresse
        invalide/pleine le reste) ;
      - 4xx generique et erreurs reseau (OSError) : backoff exponentiel borne ;
      - connexion perdue en cours de route : reconnexion, sans consommer de
        tentative ;
      - 5xx ou tentatives epuisees : echec definitif.

    Si lot est un destinataire unique, les placeholders de body sont
    personnalises pour cette adresse avant l'envoi (voir personalize()).
    `genres` est {adresse en minuscules: "M"/"F"} et `noms`
    {adresse en minuscules: (prenom, nom)} ; une adresse absente donne un
    genre/nom inconnu, pas une erreur — l'appelant est cense avoir ecarte en
    amont les destinataires non personnalisables.

    Renvoie (ok: liste d'adresses envoyees, echecs: liste de (adresse, detail)).
    Leve SendInterrupted (arret demande) ou SendThrottled (le serveur insiste).
    """
    dors = attendre or (lambda s: (time.sleep(s), True)[1])
    throttles = 0

    def patienter(raison, attempt):
        """Backoff avec demi-jitter. Leve SendInterrupted si l'appelant veut
        arreter — sans quoi une annulation resterait invisible jusqu'a 15 min."""
        plafonne = min(delay, RETRY_MAX_DELAY)
        wait = plafonne / 2 + random.uniform(0, plafonne / 2)
        print(f"  ({raison}, tentative {attempt}/{max_retries}, "
              f"nouvel essai dans {wait:.0f}s...)", file=sys.stderr)
        if not dors(wait):
            raise SendInterrupted("envoi interrompu pendant une attente de reessai")

    if len(lot) == 1:
        cle = lot[0].lower()
        body = personalize(body, lot[0], (genres or {}).get(cle),
                           (noms or {}).get(cle))
    delay = RETRY_BASE_DELAY
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            refused = conn[0].send_message(build(sender, lot, subject, body, is_html))
            ok = [a for a in lot if a not in refused]
            echecs = [(a, refused[a]) for a in refused]
            return ok, echecs
        except smtplib.SMTPServerDisconnected:
            # Ne consomme pas de tentative : une coupure n'est pas un refus, et
            # sur un serveur qui throttle elle suit immediatement un 421 —
            # la decompter epuiserait le budget sur un seul vrai refus.
            print("  (connexion perdue, reconnexion...)", file=sys.stderr)
            attempt -= 1
            conn[0] = connect_fn()
            continue
        except smtplib.SMTPRecipientsRefused as e:
            reponses = list(e.recipients.values())
            if len(lot) > 1 and any(code == 452 for code, _ in reponses):
                mid = len(lot) // 2
                print(f"  (452 sur le lot de {len(lot)} — trop de destinataires "
                      f"pour ce serveur, redecoupe en {mid}+{len(lot) - mid})",
                      file=sys.stderr)
                # Attente avant de redecouper : repartir aussitot doublerait le
                # debit a l'instant precis ou le serveur demande de ralentir.
                patienter("redecoupe du lot", attempt)
                ok1, ko1 = send_lot(conn, connect_fn, sender, lot[:mid], subject,
                                     body, is_html, max_retries, genres, noms,
                                     attendre)
                ok2, ko2 = send_lot(conn, connect_fn, sender, lot[mid:], subject,
                                     body, is_html, max_retries, genres, noms,
                                     attendre)
                return ok1 + ok2, ko1 + ko2
            # C'est ici qu'atterrissent les 421 : smtplib leve
            # SMTPRecipientsRefused des que tous les RCPT sont refuses, jamais
            # SMTPResponseException. Les traiter en echec definitif (ancien
            # comportement) marquait tout le monde en echec puis reconnectait
            # aussitot a pleine vitesse — le martelement meme qui fait bloquer
            # un compte.
            if any(est_throttling(c, m) for c, m in reponses):
                throttles += 1
                if throttles >= THROTTLE_ABANDON or attempt >= max_retries:
                    raise SendThrottled(
                        f"le serveur ralentit les envois de facon repetee "
                        f"({fmt_smtp(reponses[0])})")
                patienter("le serveur demande de ralentir", attempt)
                delay *= 2
                continue
            return [], list(e.recipients.items())
        except (smtplib.SMTPResponseException, smtplib.SMTPSenderRefused) as e:
            code = getattr(e, "smtp_code", None)
            message = getattr(e, "smtp_error", str(e))
            transitoire = code is not None and 400 <= code < 500
            if transitoire and est_throttling(code, message):
                throttles += 1
                if throttles >= THROTTLE_ABANDON or attempt >= max_retries:
                    raise SendThrottled(
                        f"le serveur ralentit les envois de facon repetee "
                        f"({fmt_smtp((code, message))})")
                patienter("le serveur demande de ralentir", attempt)
                delay *= 2
                continue
            if not transitoire or attempt == max_retries:
                return [], [(a, (code, message)) for a in lot]
            patienter(f"erreur {code} temporaire", attempt)
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
            patienter(f"erreur reseau ({e})", attempt)
            delay *= 2
    return [], [(a, (None, "tentatives epuisees")) for a in lot]


if __name__ == "__main__":
    sys.exit("send_mail.py est une bibliotheque, pas un script — lance "
              "'python app.py' pour l'interface graphique.")
