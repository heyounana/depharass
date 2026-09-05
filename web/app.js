"use strict";

// Front de l'application. Toute la logique metier (SMTP, retry, lots,
// personnalisation) vit cote Python dans send_mail.py : ce fichier ne fait
// que collecter les champs, appeler window.pywebview.api.*, et afficher ce
// qui remonte. Aucune regle metier n'est dupliquee ici — notamment la
// resolution des noms {{FIRST}}/{{LAST}}, qui reste dans send_mail.py (le CSV
// des deputes fait autorite, voir resolve_names).

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
  } else if (kind === "progress") {
    showProgress(payload);
  } else if (kind === "done") {
    sending = false;
    setCampaignButtons(false);
    stopCountdown();
    const raisons = {
      annule: "campagne arrêtée",
      throttle: "campagne interrompue par le serveur",
      erreur: "campagne interrompue sur erreur",
    };
    const p = payload || {};
    if (raisons[p.raison]) {
      // "restants" n'est pas un echec : ces destinataires n'ont jamais ete
      // tentes. Le dire, sinon un arret ressemble a une campagne terminee.
      log(`${raisons[p.raison]} — ${p.restants || 0} destinataire(s) non contacté(s)`,
        "line-err");
    }
    $("progress").hidden = true;
  }
};

// ------------------------------------------------------------- progression

let countdownTimer = null;

function stopCountdown() {
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
}

// Le compte a rebours tourne ici et pas cote Python : un evenement par seconde
// traverserait le pont pywebview 3 600 fois par heure pour rien. Python envoie
// la duree une fois, la page la decompte.
function showProgress(p) {
  const el = $("progress");
  el.hidden = false;
  stopCountdown();

  if (p.etat === "hors-plage") {
    el.textContent = "hors plage horaire — en attente";
    return;
  }

  const base = `${p.faits}/${p.total}` +
    (p.ko ? ` · ${p.ko} échec${p.ko > 1 ? "s" : ""}` : "");
  $("btn-send").textContent = `Envoi ${p.faits}/${p.total}…`;

  let reste = Math.round(p.attente || 0);
  const rendre = () => {
    if (paused) { el.textContent = `${base} · en pause`; return; }
    el.textContent = reste > 0 ? `${base} · prochain dans ${reste} s` : base;
  };
  rendre();
  if (reste > 0) {
    countdownTimer = setInterval(() => {
      if (!paused && reste > 0) reste -= 1;
      rendre();
    }, 1000);
  }
}

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
    // Liste alimentee par le bouton "+" : vide tant qu'on n'y a pas touche,
    // auquel cas Python retombe sur le champ Objet seul — le formulaire se
    // comporte alors exactement comme avant.
    subjects: subjectVariants.slice(),
    body: bodyContent(),
    isHtml: mode === "html",
    group: $("group").checked,
    batchSize: $("batch-size").value,
    durationValue: $("duration-value").value,
    durationUnit: $("duration-unit").value,
    quietStart: $("quiet-start").value,
    quietEnd: $("quiet-end").value,
    smtpHost: $("smtp-host").value,
    smtpPort: $("smtp-port").value,
    // Les lignes partent telles quelles ("adresse" ou "adresse,M") : c'est
    // Python qui les decoupe (sm.split_recipient), pour ne pas dupliquer
    // ici la regle de ce qui est un titre valide.
  };
}

// ------------------------------------------------------------------- objets

// Objets alternes entre destinataires. Tant que cette liste est vide, le champ
// Objet fonctionne comme avant ; des qu'on y ajoute quelque chose, c'est elle
// qui fait foi et chaque message tire le sien (cote Python).
let subjectVariants = [];

function refreshSubjects() {
  const liste = $("subject-list");
  liste.innerHTML = "";
  subjectVariants.forEach((texte, i) => {
    const li = document.createElement("li");
    li.className = "chip";
    const label = document.createElement("span");
    label.textContent = texte;          // jamais innerHTML : saisie utilisateur
    const retirer = document.createElement("button");
    retirer.type = "button";
    retirer.className = "chip-x";
    retirer.textContent = "×";
    retirer.title = "Retirer cet objet";
    retirer.addEventListener("click", () => {
      subjectVariants.splice(i, 1);
      refreshSubjects();
    });
    li.append(label, retirer);
    liste.appendChild(li);
  });
  liste.hidden = !subjectVariants.length;
  $("subject-hint").hidden = !subjectVariants.length;
  $("subject-hint").textContent = subjectVariants.length
    ? `${subjectVariants.length} objet${subjectVariants.length > 1 ? "s" : ""} — ` +
      `chaque message en tire un au hasard`
    : "";
}

function addSubject() {
  const texte = $("subject").value.trim();
  if (!texte) { log("objet vide", "line-err"); return; }
  if (subjectVariants.includes(texte)) { log("objet déjà dans la liste", "line-err"); return; }
  subjectVariants.push(texte);
  $("subject").value = "";
  $("subject").focus();
  refreshSubjects();
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

async function loadDeputies() {
  if (!(await ensureDeputyData())) return;
  $("deputy-groups").hidden = false;
  $("deputy-groups").open = true;   // visible d'emblee : on montre ce qui va
                                     // etre ajoute avant de l'ajouter
  syncDeputies();
  log(`${deputies.length} députés disponibles — coche/décoche un groupe pour ` +
    `filtrer la liste`, "line-muted");
}

// Simuler en deux temps : le calendrier d'abord, sans réseau, pour qu'il
// s'affiche immédiatement et avec les seuls destinataires ; l'authentification
// ensuite, seulement si expéditeur et mot de passe sont renseignés.
async function dryRun() {
  const p = collect();
  const res = await window.pywebview.api.simulate(p);
  if (res.error) { log(res.error, "line-err"); return; }

  const groupe = res.grouped;
  log(`[simulation] ${res.lots.length} message${res.lots.length > 1 ? "s" : ""} ` +
    `pour ${res.destinataires} destinataire${res.destinataires > 1 ? "s" : ""}` +
    (groupe ? " (groupés — tous ceux d'un lot se voient en To)" : ""), "line-ok");

  res.lots.forEach((lot, i) => {
    // Un séparateur au changement de jour, plutôt que la date répétée sur
    // chaque ligne : les heures restent alignées en colonne.
    if (res.days && res.days[i]) log(`  ── ${res.days[i]} ──`, "line-muted");
    const h = res.schedule && res.schedule[i] ? `${res.schedule[i]}  ` : "";
    log(`  ${h}-> ${lot.join(", ")}`, "line-muted");
  });
  if (res.fin) log(`  dernier envoi prévu vers ${res.fin}`, "line-muted");
  if (res.schedule && res.schedule.length > 1) {
    // La simulation garde l'ordre saisi pour rester relisible d'un clic à
    // l'autre, alors que l'envoi réel tire un ordre au hasard : les horaires
    // sont donc justes, mais pas forcément en face de la bonne adresse.
    log(`  (horaires indicatifs : l'ordre des destinataires est tiré au ` +
      `hasard au moment de l'envoi)`, "line-muted");
  }

  if (res.personalizedFor) {
    log(`  corps ${res.isHtml ? "HTML" : "texte brut"}, personnalisé pour ` +
      `${res.personalizedFor}`, "line-muted");
  }
  logExcluded(res.excluded);
  (res.warnings || []).forEach((w) => log(`  ⚠ ${w}`, "line-err"));

  if (res.manques && res.manques.length) {
    log(`  il manquera ${res.manques.join(", ")} pour envoyer réellement`,
      "line-muted");
  }

  // Pas d'identifiants : on s'arrête au calendrier, ce n'est pas une erreur.
  if (res.manques.includes("expéditeur") || res.manques.includes("mot de passe")) return;

  const auth = await window.pywebview.api.check_auth(p);
  if (auth.error) { log(auth.error, "line-err"); return; }
  log(`  authentification vérifiée sur ${auth.host}:${auth.port}`, "line-ok");
}

// Un destinataire dont on ne connait ni le nom ni le genre n'est plus envoye
// avec des blancs a la place ("Bonjour  Dupont") : il est ecarte en amont par
// Python et rapporte ici. Les exclus peuvent etre des centaines, donc le
// detail va dans le journal et seul le compte tient dans la confirmation.
function logExcluded(exclus) {
  if (!exclus || !exclus.length) return;
  log(`  ${exclus.length} destinataire(s) écarté(s), faute de quoi le message ` +
    `serait envoyé avec un blanc à la place du nom :`, "line-err");
  exclus.forEach((e) => log(`     ${e.addr} — ${e.raison}`, "line-err"));
}

function dureeLisible(s) {
  if (s >= 86400) return `${(s / 86400).toFixed(1)} j`;
  if (s >= 3600) return `${(s / 3600).toFixed(1)} h`;
  if (s >= 60) return `${Math.round(s / 60)} min`;
  return `${Math.round(s)} s`;
}

let paused = false;

// Pendant une campagne, Envoyer/Simuler laissent la place a Pause/Arreter :
// une campagne etalee sur des heures doit pouvoir etre reprise en main, et
// Simuler est de toute facon refuse cote Python tant qu'un envoi tourne.
function setCampaignButtons(actif) {
  paused = false;
  $("btn-send").disabled = actif;
  $("btn-send").textContent = actif ? "Envoi…" : "Envoyer";
  $("btn-dry").disabled = actif;
  $("btn-pause").hidden = !actif;
  $("btn-stop").hidden = !actif;
  $("btn-pause").textContent = "Pause";
}

async function togglePause() {
  paused = !paused;
  $("btn-pause").textContent = paused ? "Reprendre" : "Pause";
  await (paused ? window.pywebview.api.pause() : window.pywebview.api.resume());
  log(paused ? "campagne en pause" : "campagne reprise", "line-muted");
}

async function stopCampaign() {
  if (!confirm("Arrêter la campagne ? Les messages déjà envoyés le restent.")) return;
  $("btn-stop").disabled = true;
  await window.pywebview.api.cancel();
  $("btn-stop").disabled = false;
}

async function send() {
  if (sending) return;
  const p = collect();
  if (!p.dests.length) { log("aucun destinataire", "line-err"); return; }

  // Le preflight peut prendre un instant (resolution SMTP) : desactiver le
  // bouton des maintenant, sinon un double-clic lance deux campagnes.
  $("btn-send").disabled = true;
  let pre;
  try {
    pre = await window.pywebview.api.preflight(p);
  } finally {
    $("btn-send").disabled = false;
  }
  if (pre.error) { log(pre.error, "line-err"); return; }

  logExcluded(pre.excluded);

  const lignes = [`Envoyer ${pre.messages} message${pre.messages > 1 ? "s" : ""} ` +
                  `à ${pre.destinataires} destinataire${pre.destinataires > 1 ? "s" : ""} ?`];
  if (pre.duree) {
    lignes.push(`Étalé sur ${dureeLisible(pre.duree)} — un message toutes les ` +
                `${dureeLisible(pre.ecartMoyen)} en moyenne.`);
  }
  if (pre.plage) {
    const hh = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}h`;
    lignes.push(`Envois entre ${hh(pre.plage[0])} et ${hh(pre.plage[1])} seulement.`);
  }
  if (pre.objets > 1) lignes.push(`${pre.objets} objets alternés.`);
  if (pre.excluded.length) {
    lignes.push(`${pre.excluded.length} adresse(s) écartée(s) — détail dans le journal.`);
  }
  if (pre.warnings.length) lignes.push("", ...pre.warnings.map((w) => "⚠ " + w));

  if (!confirm(lignes.join("\n"))) return;

  sending = true;
  setCampaignButtons(true);

  const res = await window.pywebview.api.send(p);
  if (res.error) {
    log(res.error, "line-err");
    sending = false;
    setCampaignButtons(false);
    return;
  }
  log(`envoi lancé : ${res.dests} destinataire(s) en ${res.lots} lot(s)`);
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
  $("btn-pause").addEventListener("click", togglePause);
  $("btn-stop").addEventListener("click", stopCampaign);
  $("btn-subject-add").addEventListener("click", addSubject);
  $("btn-log-clear").addEventListener("click", () => { $("log").innerHTML = ""; });

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
