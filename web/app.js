"use strict";

// Front de l'application. Toute la logique metier (SMTP, retry, lots,
// personnalisation) vit cote Python dans send_mail.py : ce fichier ne fait
// que collecter les champs, appeler window.pywebview.api.*, et afficher ce
// qui remonte. Aucune regle metier n'est dupliquee ici — notamment la
// derivation {{FIRST}}/{{LAST}}, qui reste dans send_mail.split_name().

const $ = (id) => document.getElementById(id);

let quill = null;
let mode = "html";      // "text" | "html"
let sending = false;

// ---------------------------------------------------------------- journal

function log(text, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = text;          // jamais innerHTML : le texte vient de
  $("log").appendChild(line);       // messages serveur, potentiellement hostiles
  $("log").scrollTop = $("log").scrollHeight;
}

// Appele depuis Python via window.evaluate_js (voir Api._emit dans app.py).
window.onAppEvent = function (kind, payload) {
  if (kind === "log") {
    log(payload, "line-muted");
  } else if (kind === "ok") {
    log(payload.length > 1
      ? `OK    ${payload.length} destinataires`
      : `OK    ${payload[0]}`, "line-ok");
  } else if (kind === "fail") {
    log(`ECHEC ${payload.addr} : ${payload.detail}`, "line-err");
  } else if (kind === "error") {
    log(payload, "line-err");
  } else if (kind === "summary") {
    log(`${payload.ok} envoi(s) OK, ${payload.ko} echec(s)`);
  } else if (kind === "done") {
    sending = false;
    $("btn-send").disabled = false;
    $("btn-send").textContent = "Envoyer";
  }
};

// ------------------------------------------------------------------ champs

function bodyContent() {
  return mode === "html"
    ? (quill ? quill.root.innerHTML : "")
    : $("body-text").value;
}

function collect() {
  const dests = $("dests").value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  return {
    sender: $("sender").value,
    password: $("password").value,
    dests,
    subject: $("subject").value,
    body: bodyContent(),
    isHtml: mode === "html",
    group: $("group").checked,
    batchSize: $("batch-size").value,
    smtpHost: $("smtp-host").value,
    smtpPort: $("smtp-port").value,
    // Les lignes partent telles quelles ("adresse" ou "adresse,M") : c'est
    // Python qui les decoupe (sm.split_recipient), pour ne pas dupliquer
    // ici la regle de ce qui est un titre valide.
  };
}

function refreshCount() {
  const n = $("dests").value.split("\n").map((s) => s.trim()).filter(Boolean).length;
  $("dests-count").textContent = n ? `${n} adresse${n > 1 ? "s" : ""}` : "";
}

// -------------------------------------------------------------------- mode

function setMode(next) {
  mode = next;
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.mode === next);
  });
  $("body-text").hidden = next !== "text";
  $("body-html").hidden = next !== "html";
  // Les deux tampons sont independants : basculer ne convertit rien et ne
  // perd rien, le contenu de l'autre mode reste intact si on y revient.
}

// ----------------------------------------------------------------- actions

// Adresse seule d'une ligne "adresse" ou "adresse,M", en minuscules. Sert
// uniquement a comparer/dedoublonner cote page ; le decoupage qui fait foi
// est celui de Python (sm.split_recipient).
function addressOf(ligne) {
  return ligne.split(",")[0].trim().toLowerCase();
}

// Retire les repetitions en gardant la premiere occurrence (et sa casse).
// La comparaison porte sur l'ADRESSE seule, pas la ligne entiere : sinon
// "a@x.com" et "a@x.com,M" seraient vus comme deux destinataires.
// Insensible a la casse : le domaine l'est par definition, et aucun
// fournisseur courant ne distingue deux boites sur la casse de la partie
// locale.
function dedupe(lignes) {
  const vus = new Set();
  return lignes.filter((l) => {
    const cle = addressOf(l);
    if (vus.has(cle)) return false;
    vus.add(cle);
    return true;
  });
}

// Ajoute des adresses au champ Destinataires sans ecraser ce qui y est deja
// ni creer de doublon — recharger deux fois le meme fichier est donc sans
// effet la seconde fois.
function appendAddresses(addresses) {
  const actuelles = $("dests").value.split("\n").map((l) => l.trim()).filter(Boolean);
  $("dests").value = dedupe(actuelles.concat(addresses)).join("\n");
  refreshCount();
}

async function loadRecipients() {
  const res = await window.pywebview.api.pick_recipients_file();
  if (res.error) { log(res.error, "line-err"); return; }
  if (!res.addresses.length) return;

  // Le titre detecte est ecrit dans la ligne ("adresse,M") plutot que garde
  // a part : il devient visible et modifiable a la main dans le champ.
  const titres = res.titles || {};
  appendAddresses(res.addresses.map((a) => {
    const genre = titres[a.toLowerCase()];
    return genre ? `${a},${genre}` : a;
  }));

  if (res.isJson) {
    log(`${res.addresses.length} adresse(s) trouvée(s) dans ce fichier JSON ` +
      `(recherche dans tout le document, pas ligne par ligne)`, "line-muted");
    return;
  }

  log(`${res.addresses.length} adresse(s) chargée(s) depuis le fichier`, "line-muted");
  // Ce qui a ete ecarte est dit explicitement : l'utilisateur relit de toute
  // facon la liste dans le champ, autant qu'il sache ce qui manque.
  if (res.ignored) {
    log(`  ${res.ignored} ligne(s) sans adresse ignorée(s) (en-tête, notes…)`,
      "line-muted");
  }
  if (res.multi) {
    log(`  ${res.multi} ligne(s) contenaient plusieurs adresses : seule la ` +
      `première de chaque ligne a été retenue`, "line-muted");
  }

  log(`  ${Object.keys(titres).length} titre(s) détecté(s) sur ` +
    `${res.addresses.length} adresse(s)`, "line-muted");
}

// Liste complete des deputes, chargee une fois (voir load_deputy_data cote
// Python). deputyManaged retient les adresses que LES CASES ont ajoutees :
// c'est la seule chose que la synchro s'autorise a retirer. Se fier au simple
// fait qu'une ligne "est une adresse de depute" ne suffirait pas — une
// adresse tapee a la main avant le chargement disparaitrait en decochant son
// groupe, alors que l'utilisateur ne l'a jamais confiee au mecanisme.
let deputies = [];                    // [{email, sigle, genre}] ordre du fichier
let deputyManaged = new Set();        // adresses (minuscules) ajoutees par les cases
let deputyDataLoaded = false;

function selectedDeputyGroups() {
  return Array.from(document.querySelectorAll("#deputy-groups-list input:checked"))
    .map((el) => el.value);
}

// Reecrit le champ Destinataires. Deux regles :
//   - on ne retire que les adresses que les cases ont ajoutees (deputyManaged),
//     donc tout le reste — saisi a la main, charge d'un fichier — survit ;
//   - on n'ajoute pas une adresse deja presente, donc jamais de doublon.
// Consequence : une adresse de depute tapee a la main reste dans le champ
// meme si on decoche son groupe, puisqu'elle n'a jamais ete "prise en charge".
function syncDeputies() {
  const coches = new Set(selectedDeputyGroups());
  // Comparaisons sur l'adresse seule : une ligne peut porter un titre
  // ("adresse,M"), la ligne entiere ne correspondrait a rien.
  const autres = $("dests").value
    .split("\n").map((l) => l.trim()).filter(Boolean)
    .filter((l) => !deputyManaged.has(addressOf(l)));

  const deja = new Set(autres.map(addressOf));
  const ajoutes = deputies
    .filter((d) => coches.has(d.sigle) && !deja.has(d.email.toLowerCase()))
    .map((d) => (d.genre ? `${d.email},${d.genre}` : d.email));

  deputyManaged = new Set(ajoutes.map(addressOf));
  $("dests").value = dedupe(autres.concat(ajoutes)).join("\n");
  refreshCount();

  const selectionnes = deputies.filter((d) => coches.has(d.sigle)).length;
  $("deputy-groups-summary").textContent =
    `${selectionnes} / ${deputies.length} députés`;
}

// Charge les donnees sans rien reveler a l'ecran : appelable au demarrage
// pour que les titres des deputes soient connus meme si l'utilisateur ne
// clique jamais sur "Charger les deputes" (adresse tapee a la main, ou
// venant d'un fichier). `silencieux` evite de polluer le journal au
// demarrage si le CSV est absent — c'est une fonctionnalite optionnelle.
async function ensureDeputyData(silencieux = false) {
  if (deputyDataLoaded) return true;
  const res = await window.pywebview.api.load_deputy_data();
  if (res.error) {
    if (!silencieux) log(res.error, "line-err");
    return false;
  }

  deputies = res.deputies;

  const list = $("deputy-groups-list");
  list.innerHTML = "";
  for (const g of res.groups) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = g.sigle;
    cb.checked = true;   // tout coche par defaut : un clic charge tout le monde
    cb.addEventListener("change", syncDeputies);
    const texte = document.createElement("span");
    texte.textContent = `${g.libelle} (${g.count})`;
    label.append(cb, texte);
    list.appendChild(label);
  }
  deputyDataLoaded = true;
  return true;
}

// Le panneau des groupes peut deborder de #form-grid (grille bornee par le
// flex parent, cf. style.css) : plutot que de laisser sa propre scrollbar
// interne s'activer sur un petit rectangle, on agrandit la fenetre pile de
// ce qu'il faut. Sans effet si tout tient deja (overflow <= 0).
function growToFit() {
  const grid = $("form-grid");
  const overflow = grid.scrollHeight - grid.clientHeight;
  if (overflow > 0) {
    window.pywebview.api.grow_window(overflow + 4);   // marge de securite
  }
}

async function loadDeputies() {
  if (!(await ensureDeputyData())) return;
  $("deputy-groups").hidden = false;
  $("deputy-groups").open = true;   // visible d'emblee : on montre ce qui va
                                     // etre ajoute avant de l'ajouter
  syncDeputies();
  growToFit();
  log(`${deputies.length} députés disponibles — coche/décoche un groupe pour ` +
    `filtrer la liste`, "line-muted");
}

async function dryRun() {
  // dry_run verifie une vraie authentification (connexion + login, fermee
  // aussitot) : un mot de passe est donc requis ici aussi, pas seulement
  // pour Envoyer.
  const res = await window.pywebview.api.dry_run(collect());
  if (res.error) { log(res.error, "line-err"); return; }
  log(`[simulation] authentification vérifiée sur ${res.host}:${res.port} — ` +
    `${res.isHtml ? "HTML" : "texte"}`, "line-ok");
  res.lots.forEach((lot) => log(`  -> ${lot.join(", ")}`, "line-muted"));
  if (res.personalizedFor) {
    log(`  corps personnalisé pour ${res.personalizedFor}`, "line-muted");
  }
  warnMissingGender(res.missingGender);
}

// {{TITLE}}/{{TERM}} n'ont de valeur que pour un destinataire dont le genre
// est connu (les députés du fichier). Pour les autres ils deviennent des
// blancs : on le dit, plutôt que de laisser partir "Bonjour  Dupont".
function warnMissingGender(n) {
  if (!n) return;
  log(`  ${n} destinataire(s) sans genre connu : {{TITLE}}/{{TERM}} seront ` +
    `remplacés par du vide pour eux`, "line-err");
}

async function send() {
  if (sending) return;
  const p = collect();
  const n = p.dests.length;
  if (!n) { log("aucun destinataire", "line-err"); return; }
  if (!confirm(`Envoyer à ${n} destinataire${n > 1 ? "s" : ""} ?`)) return;

  sending = true;
  $("btn-send").disabled = true;
  $("btn-send").textContent = "Envoi…";

  const res = await window.pywebview.api.send(p);
  if (res.error) {
    log(res.error, "line-err");
    sending = false;
    $("btn-send").disabled = false;
    $("btn-send").textContent = "Envoyer";
    return;
  }
  log(`envoi lancé : ${res.dests} destinataire(s) en ${res.lots} lot(s)`);
  warnMissingGender(res.missingGender);
}

// -------------------------------------------------------------- demarrage

function wire() {
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => setMode(t.dataset.mode));
  });

  // Meme nettoyage que la version CLI : Google affiche les mots de passe
  // d'application par blocs de 4 separes par des espaces.
  $("password").addEventListener("input", (e) => {
    const clean = e.target.value.replace(/\s+/g, "");
    if (clean !== e.target.value) e.target.value = clean;
  });

  $("dests").addEventListener("input", refreshCount);
  $("btn-load").addEventListener("click", loadRecipients);
  $("btn-load-deputies").addEventListener("click", loadDeputies);
  $("btn-dry").addEventListener("click", dryRun);
  $("btn-send").addEventListener("click", send);
  $("btn-log-clear").addEventListener("click", () => { $("log").innerHTML = ""; });

  // Repli du "toggle" natif : couvre aussi le cas ou le panneau est
  // referme puis rouvert a la main (clic sur son <summary>), pas seulement
  // le premier chargement gere directement dans loadDeputies().
  $("deputy-groups").addEventListener("toggle", () => {
    if ($("deputy-groups").open) growToFit();
  });

  $("body-text").placeholder = BODY_PLACEHOLDER;
}

// Echelle de tailles pour les boutons A-/A+ (13px = taille par defaut de
// Quill, cf. .ql-container dans quill.snow.css — les pas se font donc autour
// de la vraie valeur de depart, pas d'une valeur arbitraire).
const FONT_SIZES = ["10px", "12px", "13px", "14px", "16px", "18px", "20px",
  "24px", "28px", "32px", "36px", "48px"];
const DEFAULT_FONT_SIZE = "13px";

const FONT_FAMILIES = ["Arial", "Georgia", "Times New Roman", "Courier New",
  "Verdana", "Tahoma", "Trebuchet MS"];

// Exemple concret plutot qu'une description abstraite des placeholders :
// on montre a quoi ressemble un vrai message. Le saut de ligne passe tel
// quel dans l'attribut HTML placeholder (textarea) et dans data-placeholder
// (Quill, dont le CSS de base heritee white-space:pre-wrap depuis
// .ql-editor jusqu'a son ::before) : les deux le rendent comme un retour
// a la ligne, pas besoin de <br> ni de regle CSS supplementaire.
const BODY_PLACEHOLDER =
  "Bonjour {{TITLE}} {{LAST}},\nEn tant qu'adhérent{{TERM}}...";

function stepFontSize(delta) {
  const range = quill.getSelection(true);
  const current = quill.getFormat(range).size || DEFAULT_FONT_SIZE;
  let i = FONT_SIZES.indexOf(current);
  if (i === -1) i = FONT_SIZES.indexOf(DEFAULT_FONT_SIZE);
  i = Math.min(FONT_SIZES.length - 1, Math.max(0, i + delta));
  quill.format("size", FONT_SIZES[i]);
}

function initEditor() {
  if (typeof Quill === "undefined") {
    // Assets absents : le mode HTML reste inutilisable, le mode texte marche.
    // Renvoie false pour que l'appelant demarre quand meme sur un champ
    // exploitable plutot que sur cet ecran vide.
    $("quill-missing").hidden = false;
    $("editor").hidden = true;
    return false;
  }

  // Par defaut Quill restreint size/font a 3 classes fixes (small/large/huge,
  // serif/monospace) : on bascule sur les attributs de style CSS pour des
  // valeurs libres (px exacts, polices choisies), bornees par une whitelist
  // explicite plutot que d'accepter n'importe quelle chaine.
  const Size = Quill.import("attributors/style/size");
  Size.whitelist = FONT_SIZES;
  Quill.register(Size, true);

  const Font = Quill.import("attributors/style/font");
  Font.whitelist = FONT_FAMILIES;
  Quill.register(Font, true);

  quill = new Quill("#editor", {
    theme: "snow",
    placeholder: BODY_PLACEHOLDER,
    modules: {
      toolbar: [
        [{ header: [1, 2, 3, false] }],
        ["bold", "italic", "underline", "strike"],
        [{ font: FONT_FAMILIES }],
        [{ color: [] }, { background: [] }],
        [{ list: "ordered" }, { list: "bullet" }],
        [{ align: [] }],
        ["link", "blockquote"],
        ["clean"],
      ],
    },
  });

  // Boutons A-/A+ : pas de picker Quill standard pour un pas incremental,
  // on les insere directement dans la barre d'outils deja generee par Quill
  // pour qu'ils s'alignent visuellement avec le reste.
  const toolbar = quill.getModule("toolbar").container;
  const group = document.createElement("span");
  group.className = "ql-formats";
  const minus = document.createElement("button");
  minus.type = "button";
  minus.className = "ql-size-step";
  minus.textContent = "A−";
  minus.title = "Réduire la taille";
  minus.addEventListener("click", () => stepFontSize(-1));
  const plus = document.createElement("button");
  plus.type = "button";
  plus.className = "ql-size-step";
  plus.textContent = "A+";
  plus.title = "Agrandir la taille";
  plus.addEventListener("click", () => stepFontSize(1));
  group.append(minus, plus);
  toolbar.insertBefore(group, toolbar.children[1] || null);
  return true;
}

const quillReady = initEditor();
wire();
// HTML par defaut, sauf si Quill n'a pas pu se charger : demarrer sur un
// panneau vide serait pire que revenir au mode texte, toujours exploitable.
setMode(quillReady ? "html" : "text");

// Les appels api.* ne sont possibles qu'une fois le pont pret.
const actions = ["btn-load", "btn-load-deputies", "btn-dry", "btn-send"];
actions.forEach((id) => { $(id).disabled = true; });
window.addEventListener("pywebviewready", () => {
  actions.forEach((id) => { $(id).disabled = false; });
});
