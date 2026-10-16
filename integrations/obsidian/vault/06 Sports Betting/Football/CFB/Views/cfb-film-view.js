// One game is one source of writing. Team pages read it with its opponent/context.
const BASE="06 Sports Betting/Football/CFB",LEAGUE="cfb",current=dv.current();
const helperFile=app.vault.getAbstractFileByPath(`${BASE}/Views/cfb-film.js`);
if(!helperFile){dv.paragraph("Game-note helper has not synced yet.");return;}
const helper=new Function(await app.vault.read(helperFile))(),norm=helper.norm;
const date=v=>v?.toFormat?v.toFormat("yyyy-MM-dd"):String(v||"").slice(0,10);
const fold=(title,text)=>`> [!abstract]- ${title}\n${text.split("\n").map(l=>"> "+l).join("\n")}`;
function web(url,label){try{const u=new URL(String(url));if(!["http:","https:"].includes(u.protocol))return "";return `[${label}](<${u.href.replace(/[<>\s]/g,encodeURIComponent)}>)`;}catch{return "";}}
if(input?.mode==="compare"){
  if(current.site_url)dv.paragraph(web(current.site_url,"Open this game on the site"));
  const source=app.vault.getAbstractFileByPath(norm(current.source_matchup)+".md");
  if(!source){dv.paragraph("No site comparison attached. Keep writing here; a site import can be linked later using Game Notes Template.");return;}
  const raw=await app.vault.read(source),match=raw.match(/<!-- cfb:engine-brief:start -->([\s\S]*?)<!-- cfb:engine-brief:end -->/);
  const properties=app.metadataCache.getFileCache(source)?.frontmatter||{};
  const stamp=String(properties.engine_imported_at||"").slice(0,10)||"date unavailable";
  if(match)dv.paragraph(fold(`Site comparison · imported ${stamp}`,match[1].trim()+`\n\n${dv.fileLink(source.path,false,"Full site evidence and earlier research")}\n\nCached site data, separate from your tape notes. Run Game Notes Template with the same URL/ID to refresh it. Historical imports may include retrospective information.`));
  return;
}
const self=norm(current.file.path),games=Array.from(dv.pages(`"${BASE}/Games"`)).filter(p=>p.type===`${LEAGUE}-game`&&[p.home_team,p.away_team].some(t=>norm(t)===self)&&(!current.season||String(current.season)===String(p.season))).sort((a,b)=>date(b.game_date).localeCompare(date(a.game_date))||a.file.path.localeCompare(b.file.path));
const teamURL=current.engine_team_id==null?"":LEAGUE==="cfb"?`https://cfb-intelligence.onrender.com/college-football/teams/${encodeURIComponent(current.engine_team_id)}/`:`https://cfb-intelligence.onrender.com/nfl/teams/${encodeURIComponent(current.engine_team_id)}/?season=${current.season||new Date().getFullYear()}`;
if(teamURL)dv.paragraph(web(teamURL,"Compare this team on the site"));
let shown=0;
for(const game of games){
  const side=norm(game.home_team)===self?"home":"away",opponent=side==="home"?game.away_team:game.home_team;
  const file=app.vault.getAbstractFileByPath(game.file.path);if(!file)continue;
  const sections=helper.sections(await app.vault.read(file)),notes=sections[side];
  if(!Object.values(notes).some(helper.hasNotes))continue;
  shown++;
  dv.header(2,`${date(game.game_date)} · vs ${norm(opponent).split("/").pop()}`);
  dv.paragraph(`${dv.fileLink(game.file.path,false,"Open / edit game notes")} · Opponent: ${opponent}${game.week?" · Week "+game.week:""} · ${side==="home"?"Designated home":"Designated away"}`);
  if(helper.hasNotes(sections.context))dv.paragraph(`**Game context**\n\n${sections.context}`);
  for(const category of ["offense","defense","context"])if(helper.hasNotes(notes[category])){dv.header(3,category[0].toUpperCase()+category.slice(1));dv.paragraph(notes[category]);}
}
if(!shown)dv.paragraph("No game notes for this team yet. Open a game from the notebook and write directly under this team's headings. These notes will appear here automatically.");
// Earlier atomic research stays distinct: its source matchup is not silently reassigned to a watched game.
const legacy=Array.from(dv.pages(`"${BASE}/Observations"`)).filter(p=>p.type===`${LEAGUE}-observation`&&norm(p.team)===self&&(!current.season||String(p.season)===String(current.season))).sort((a,b)=>date(b.game_date).localeCompare(date(a.game_date)));
if(legacy.length){
  const text=legacy.map(p=>`**${p.observation||p.file.name}**\n\n${dv.fileLink(p.file.path,false,"Original note and evidence")} · ${p.matchup} · Opponent: ${p.opponent} · ${p.category||"context"} · ${date(p.game_date)}`).join("\n\n");
  dv.paragraph(fold("Earlier research — original game associations preserved",text));
}
