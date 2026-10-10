// Compact display of atomic insights; editing opens the capture form or note Properties.
const BASE="06 Sports Betting/Football/CFB";
const current=dv.current(),norm=v=>String(v?.path??v??"").replace(/^\[\[|\]\]$/g,"").split("|")[0].replace(/\.md$/,"");
const obs=Array.from(dv.pages(`"${BASE}/Observations"`)).filter(p=>p.type==="cfb-observation");
const isTeam=current.type==="cfb-team",isDashboard=current.type==="cfb-dashboard";
let records=obs.filter(p=>isDashboard?(!current.season||String(p.season)===String(current.season)):
  isTeam?norm(p.team)===norm(current.file.path)&&(!current.season||String(p.season)===String(current.season)):
  norm(p.matchup)===norm(current.file.path));
if(input?.predictions)records=records.filter(p=>String(p.prediction||"").trim());
if(input?.side)records=records.filter(p=>p.side===input.side);
if(input?.category)records=records.filter(p=>p.category===input.category);
if(input?.active)records=records.filter(p=>["active","watch"].includes(p.carry));
records.sort((a,b)=>String(b.game_date).localeCompare(String(a.game_date))||a.file.path.localeCompare(b.file.path));
async function launch(options){
  try {
    const file=app.vault.getAbstractFileByPath(`${BASE}/Views/cfb-observations.js`);
    if(!file)throw new Error("Observation helper has not synced.");
    // Dataview may expose Electron's loader, which cannot resolve Obsidian.
    // Reuse the host API that Templater supplies as tp.obsidian.
    const generator=app.plugins?.plugins?.["templater-obsidian"]?.templater?.functions_generator;
    const obsidian=generator?.additional_functions?.().obsidian;
    if(!obsidian?.Modal||!obsidian?.Setting||!obsidian?.parseYaml)
      throw new Error("Enable Templater and wait for it to load, then reopen this note");
    await new Function(await app.vault.read(file))().open(app,obsidian,options);
  }catch(error){dv.paragraph(`Could not open form: ${error.message||error}. Use CFB Observation Template from the dashboard.`);}
}
if(input?.editOnly){
  dv.paragraph(`**${current.observation||"Observation"}**\n\n${current.team} · Opponent: ${current.opponent} · Matchup: ${current.matchup}`);
  const button=dv.container.createEl("button",{text:"Edit observation"});
  button.addEventListener("click",()=>launch({observationPath:current.file.path}));return;
}
if(current.type!=="cfb-pregame-snapshot") {
  const button=dv.container.createEl("button",{text:"+ Add observation"});
  button.addEventListener("click",()=>launch({matchupPath:!isTeam&&!isDashboard?current.file.path:undefined,teamPath:isTeam?current.file.path:undefined,side:input?.side,category:input?.category}));
}
if(!records.length){dv.paragraph(input?.active?"No active/watch observations yet.":"No observations yet. Add an insight when the evidence supports one.");return;}
const sign={positive:"+",negative:"−",neutral:"○",mixed:"±"};
for(const p of records){
  // Paragraphs keep full text readable; the management grid is available separately.
  const today=dv.date("today").toFormat("yyyy-MM-dd"),review=String(p.review||"");
  const due=/^\d{4}-\d{2}-\d{2}/.test(review)&&review.slice(0,10)<=today?" · REVIEW DUE":"";
  const actions=[p.implication&&`**Implication:** ${p.implication}`,p.applies_when&&`Applies when: ${p.applies_when}`,p.invalidated_by&&`Invalidated by: ${p.invalidated_by}`,p.next_check&&`Next check: ${p.next_check}`,p.resolution&&`Resolution: ${p.resolution}`].filter(Boolean);
  if(input?.predictions)actions.push(`**Prediction:** ${p.prediction}${p.probability!=null?` · ${p.probability}%`:""}`,`Result: ${p.prediction_result||"unresolved"}${p.actual?" · Actual: "+p.actual:""}`);
  if(actions.length)dv.paragraph(actions.join("\n\n"));
  dv.paragraph(`**${sign[p.direction]||"○"} ${p.observation||p.file.name}**\n\n${dv.fileLink(p.file.path,false,"Evidence / full note")} · ${p.carry||"game-only"}${review?" · Review: "+review:""}${due}`+
    (isTeam||isDashboard?`\n\n${p.team} · ${p.category} · ${p.matchup} · Opponent: ${p.opponent}${p.team_line!=null?" · Team spread "+p.team_line:""}${p.total!=null?" · Total "+p.total:""}${p.context?" · "+p.context:""}`:""));
  const edit=dv.container.createEl("button",{text:"Edit"});edit.addEventListener("click",()=>launch({observationPath:p.file.path}));
}
