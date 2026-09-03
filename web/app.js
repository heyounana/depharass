"use strict";

// Front de l'application. Toute la logique metier (SMTP, retry, lots,
// personnalisation) vit cote Python dans send_mail.py : ce fichier ne fait
// que collecter les champs, appeler window.pywebview.api.*, et afficher ce
// qui remonte. Aucune regle metier n'est dupliquee ici — notamment la
// derivation {{FIRST}}/{{LAST}}, qui reste dans send_mail.split_name().

const $ = (id) => document.getElementById(id);

let quill = null;
let mode = "text";      // "text" | "html"
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
    // Seuls les titres attribues a la main partent : Python connait deja
    // ceux du CSV et les fusionne dessous (voir _prepare).
    titles: Object.fromEntries(titleOverrides),
  };
}

function refreshCount() {
  const n = $("dests").value.split("\n").map((s) => s.trim()).filter(Boolean).length;
  $("dests-count").textContent = n ? `${n} adresse${n > 1 ? "s" : ""}` : "";
  refreshTitles();
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

// Retire les repetitions en gardant la premiere occurrence (et sa casse).
// La comparaison est insensible a la casse : la partie domaine l'est par
// definition, et aucun fournisseur courant ne distingue deux boites sur la
// seule casse de la partie locale.
function dedupe(lignes) {
  const vus = new Set();
  return lignes.filter((l) => {
    const cle = l.toLowerCase();
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
  appendAddresses(res.addresses);

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
}

// Liste complete des deputes, chargee une fois (voir load_deputy_data cote
// Python). deputyManaged retient les adresses que LES CASES ont ajoutees :
// c'est la seule chose que la synchro s'autorise a retirer. Se fier au simple
// fait qu'une ligne "est une adresse de depute" ne suffirait pas — une
// adresse tapee a la main avant le chargement disparaitrait en decochant son
// groupe, alors que l'utilisateur ne l'a jamais confiee au mecanisme.
let deputies = [];                    // [{email, sigle, genre}] ordre du fichier
let deputyManaged = new Set();        // adresses (minuscules) ajoutees par les cases
let deputyGenders = new Map();        // adresse (minuscules) -> "M"/"F", depuis le CSV
let deputyDataLoaded = false;

// Titres attribues a la main dans le panneau Titres. Ils priment sur le CSV
// (on peut donc corriger un depute sans toucher au fichier) et survivent au
// retrait d'une adresse du champ, pour ne pas etre perdus si elle revient.
let titleOverrides = new Map();       // adresse (minuscules) -> "M"/"F"

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
  const autres = $("dests").value
    .split("\n").map((l) => l.trim()).filter(Boolean)
    .filter((l) => !deputyManaged.has(l.toLowerCase()));

  const deja = new Set(autres.map((l) => l.toLowerCase()));
  const ajoutes = deputies
    .filter((d) => coches.has(d.sigle) && !deja.has(d.email.toLowerCase()))
    .map((d) => d.email);

  deputyManaged = new Set(ajoutes.map((e) => e.toLowerCase()));
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
  deputyGenders = new Map(
    deputies.filter((d) => d.genre).map((d) => [d.email.toLowerCase(), d.genre]));

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

async function loadDeputies() {
  if (!(await ensureDeputyData())) return;
  $("deputy-groups").hidden = false;
  $("deputy-groups").open = true;   // visible d'emblee : on montre ce qui va
                                     // etre ajoute avant de l'ajouter
  syncDeputies();
  log(`${deputies.length} députés disponibles — coche/décoche un groupe pour ` +
    `filtrer la liste`, "line-muted");
}

// ------------------------------------------------------------------ titres

function recipientLines() {
  return $("dests").value.split("\n").map((l) => l.trim()).filter(Boolean);
}

// Titre effectif : ce qui a ete attribue a la main prime sur le CSV.
function titleFor(addr) {
  const cle = addr.toLowerCase();
  return titleOverrides.get(cle) || deputyGenders.get(cle) || null;
}

function setTitle(addr, genre) {
  const cle = addr.toLowerCase();
  if (genre) titleOverrides.set(cle, genre);
  else titleOverrides.delete(cle);   // "—" retire l'override, le CSV reprend
}

function refreshTitles() {
  const dests = recipientLines();
  const sans = dests.filter((d) => !titleFor(d));

  $("titles-count").textContent = dests.length
    ? `${dests.length - sans.length} avec titre · ${sans.length} sans titre`
    : "aucun destinataire";

  // Les lignes ne sont construites que si le panneau est ouvert : charger
  // 577 deputes ne doit pas fabriquer 577 lignes pour rien.
  const panneau = $("titles-panel");
  const liste = $("titles-list");
  if (!panneau.open) { liste.innerHTML = ""; return; }

  const visibles = $("titles-only-missing").checked ? sans : dests;
  liste.innerHTML = "";
  for (const addr of visibles) {
    const genre = titleFor(addr);
    const ligne = document.createElement("div");
    ligne.className = "title-row" + (genre ? "" : " is-missing");

    const select = document.createElement("select");
    for (const [val, libelle] of [["", "—"], ["M", "M."], ["F", "Mme."]]) {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = libelle;
      opt.selected = (genre || "") === val;
      select.appendChild(opt);
    }
    select.addEventListener("change", () => {
      setTitle(addr, select.value);
      refreshTitles();
    });

    const texte = document.createElement("span");
    texte.className = "addr";
    texte.textContent = addr;
    ligne.append(select, texte);

    // D'ou vient le titre : utile pour distinguer le CSV d'un choix manuel.
    const source = titleOverrides.has(addr.toLowerCase()) ? "manuel"
      : (deputyGenders.has(addr.toLowerCase()) ? "fichier députés" : "");
    if (source) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = source;
      ligne.appendChild(tag);
    }
    liste.appendChild(ligne);
  }
}

// Applique un titre a toutes les lignes actuellement affichees (donc au
// filtre courant) : coller 20 adresses d'un meme genre = deux clics.
function bulkTitle(genre) {
  const dests = recipientLines();
  const cibles = $("titles-only-missing").checked
    ? dests.filter((d) => !titleFor(d))
    : dests;
  cibles.forEach((d) => setTitle(d, genre));
  refreshTitles();
  log(`${cibles.length} titre(s) définis sur ${genre === "M" ? "M." : "Mme."}`,
    "line-muted");
}

async function dryRun() {
  const res = await window.pywebview.api.dry_run(collect());
  if (res.error) { log(res.error, "line-err"); return; }
  log(`[simulation] via ${res.host}:${res.port} — ${res.isHtml ? "HTML" : "texte"}`);
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

  // Les lignes ne sont construites qu'a l'ouverture du panneau (voir
  // refreshTitles), d'ou l'ecoute de 'toggle'.
  $("titles-panel").addEventListener("toggle", refreshTitles);
  $("titles-only-missing").addEventListener("change", refreshTitles);
  $("btn-title-all-m").addEventListener("click", () => bulkTitle("M"));
  $("btn-title-all-f").addEventListener("click", () => bulkTitle("F"));
}

function initEditor() {
// Echelle de tailles pour les boutons A-/A+ (13px = taille par defaut de
// Quill, cf. .ql-container dans quill.snow.css — les pas se font donc autour
// de la vraie valeur de depart, pas d'une valeur arbitraire).
const FONT_SIZES = ["10px", "12px", "13px", "14px", "16px", "18px", "20px",
  "24px", "28px", "32px", "36px", "48px"];
const DEFAULT_FONT_SIZE = "13px";

const FONT_FAMILIES = ["Arial", "Georgia", "Times New Roman", "Courier New",
  "Verdana", "Tahoma", "Trebuchet MS"];

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
    $("quill-missing").hidden = false;
    $("editor").hidden = true;
    return;
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
}

initEditor();
wire();
setMode("text");

// Les appels api.* ne sont possibles qu'une fois le pont pret.
const actions = ["btn-load", "btn-load-deputies", "btn-dry", "btn-send"];
actions.forEach((id) => { $(id).disabled = true; });
window.addEventListener("pywebviewready", () => {
  actions.forEach((id) => { $(id).disabled = false; });
  // Chargement silencieux : le compteur de titres doit etre juste des le
  // depart, y compris pour une adresse de depute tapee a la main sans etre
  // passee par le bouton. Sans effet visible si le fichier est absent.
  ensureDeputyData(true).then(refreshTitles);
});
