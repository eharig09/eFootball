// Local-only template factory. No network calls; never overwrites existing team pages.
const BASE = "06 Sports Betting/Football/CFB";
const q = value => JSON.stringify(value);
const cleanName = value => {
  const name = String(value ?? "").trim().replace(/\s+/g, " ");
  if (!name || /[\\/:*?"<>|#\[\]\r\n]/.test(name) || /[. ]$/.test(name))
    throw new Error("Use a team name without filename/link punctuation, such as Michigan or Miami FL.");
  return name;
};
async function folder(tp, path) {
  let current = "";
  for (const part of path.split("/")) {
    current = current ? `${current}/${part}` : part;
    if (!tp.app.vault.getAbstractFileByPath(current)) await tp.app.vault.createFolder(current);
  }
}
async function prompt(tp, label, value = "") {
  const answer = await tp.system.prompt(label, value, true);
  if (answer === null) throw new Error("Cancelled.");
  return answer.trim();
}
async function seasonPrompt(tp, fallback) {
  const year = await prompt(tp, "Season (calendar year the season begins)", String(fallback));
  if (!/^\d{4}$/.test(year)) throw new Error("Season must be a four-digit year.");
  return Number(year);
}
async function selectTeam(tp, label) {
  const existing = tp.app.vault.getMarkdownFiles()
    .filter(f => f.path.startsWith(`${BASE}/Teams/`)).map(f => f.basename)
    .sort((a,b)=>a.localeCompare(b));
  const selected = await tp.system.suggester(["+ Add a new team", ...existing], [null, ...existing], true, label);
  return cleanName(selected === null ? await prompt(tp, `${label} — team name`) : selected);
}
async function body(tp, name, season) {
  const f = tp.app.vault.getAbstractFileByPath(`${BASE}/Views/team-page.md`);
  if (!f) throw new Error("Team page blueprint has not synced yet.");
  return (await tp.app.vault.read(f)).replaceAll("__TEAM_JSON__", q(name))
    .replaceAll("__TEAM_NAME__", name).replaceAll("__SEASON__", String(season));
}
async function ensureTeam(tp, name, season) {
  await folder(tp, `${BASE}/Teams`);
  const path = `${BASE}/Teams/${name}.md`;
  if (!tp.app.vault.getAbstractFileByPath(path)) await tp.app.vault.create(path, await body(tp,name,season));
  return `[[${path.slice(0,-3)}]]`;
}
function teamSection(side, name, link) {
  let text = `## ${side === "home" ? "Home" : "Away"} team\n\n${link}\n\n`;
  for (const category of ["Offense", "Defense", "Context"]) {
    const marker = `${side}-${category.toLowerCase()}`;
    text += `### ${category}\n\n<!-- cfb:${marker}:start -->\n`;
    text += "| + | - | Evidence / condition | Carry forward | Review / expires |\n";
    text += "| --- | --- | --- | --- | --- |\n| | | | game-only | |\n";
    text += `<!-- cfb:${marker}:end -->\n\n`;
  }
  return text;
}
async function matchup(tp) {
  if (tp.file.content.replace(/<%[\s\S]*?%>/g, "").trim()) throw new Error("Use Create new note from template, or insert into an empty note.");
  const date = await prompt(tp,"Game date (YYYY-MM-DD)",tp.date.now("YYYY-MM-DD"));
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || new Date(date+"T12:00:00Z").toISOString().slice(0,10)!==date)
    throw new Error("Enter a valid date in YYYY-MM-DD format.");
  const season = await seasonPrompt(tp,date.slice(0,4));
  const week = await prompt(tp,"Week (number, bowls, or playoff)","");
  const away = await selectTeam(tp,"Away team / designated away at a neutral site");
  const home = await selectTeam(tp,"Home team / designated home at a neutral site");
  if (away.toLowerCase()===home.toLowerCase()) throw new Error("Home and away teams must differ.");
  const name = `${date} - ${away} at ${home}`;
  const target = `${BASE}/Matchups/${name}`;
  const existing = tp.app.vault.getAbstractFileByPath(target+".md");
  if (existing && existing.path !== tp.file.path(true)) throw new Error("This matchup already exists. Open the existing note.");
  // Validate and load all dependencies before creating pages.
  await body(tp,away,season);
  const awayLink = await ensureTeam(tp,away,season);
  const homeLink = await ensureTeam(tp,home,season);
  await folder(tp,`${BASE}/Matchups`);
  await tp.file.move(target);
  let text = `---\ntype: cfb-matchup\nseason: ${season}\nweek: ${q(week)}\ngame_date: ${q(date)}\n`;
  text += `home_team: ${q(homeLink)}\naway_team: ${q(awayLink)}\n`;
  text += `line:\ntotal:\nprice:\nprice_market: ""\nprice_selection: ""\nhome_spread_price:\naway_spread_price:\nover_price:\nunder_price:\nhome_moneyline:\naway_moneyline:\nsportsbook: ""\nmarket_timestamp: ""\nneutral_site: false\nvenue: ""\nkickoff: ""\ngame_context: ""\nstatus: research\nnext_action: ""\nreview_date:\nhome_score:\naway_score:\ntags:\n  - football/cfb/matchup\n---\n\n`;
  text += `# ${away} at ${home}\n\n**Away:** ${awayLink} · **Home:** ${homeLink}\n\n`;
  text += `[[${BASE}/CFB Dashboard|CFB Dashboard]] · [[${BASE}/CFB Setup Guide|Instructions]]\n\n`;
  text += "> `line` is the HOME spread (negative = home favored); away spread is its opposite. `total` is combined points. `price` is American odds for `price_market` + `price_selection`. Record the sportsbook and timestamp. + / - are favorable / unfavorable observations ABOUT THE TEAM, not automatic bet signals.\n\n";
  text += "## Game context\n\n<!-- cfb:game-context:start -->\n- Weather / surface:\n- Rest / travel / scheduling:\n- Stakes / rivalry / situational factors:\n- Pace / expected game script:\n- Injury / lineup news:\n<!-- cfb:game-context:end -->\n\n";
  text += teamSection("away",away,awayLink) + teamSection("home",home,homeLink);
  text += "## Decision and next action\n\n- Thesis:\n- Conflicting evidence:\n- Price / information needed to act:\n- Decision (watch / pass / bet):\n- Pregame conclusion and timestamp:\n\n## Postgame review\n\n- What happened:\n- Which observations held up:\n- Which observations changed:\n- Carry-forward rows updated:\n\n## Sources\n\n- \n";
  const helper=tp.app.vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);
  if(helper)text=new Function(await tp.app.vault.read(helper))().organize(text,tp.obsidian.parseYaml);
  return text;
}
async function team(tp) {
  if (tp.file.content.replace(/<%[\s\S]*?%>/g, "").trim()) throw new Error("Use this template in an empty new note.");
  const name = cleanName(await prompt(tp,"Team name"));
  const season = await seasonPrompt(tp,tp.date.now("YYYY"));
  const target = `${BASE}/Teams/${name}`;
  const existing = tp.app.vault.getAbstractFileByPath(target+".md");
  if (existing && existing.path!==tp.file.path(true)) throw new Error("Team page already exists; open that page.");
  const result = await body(tp,name,season);
  await folder(tp,`${BASE}/Teams`);
  await tp.file.move(target);
  return result;
}
return {matchup, team};
