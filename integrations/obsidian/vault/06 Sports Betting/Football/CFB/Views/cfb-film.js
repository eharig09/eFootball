// Game-centered notes: prose is the record; team views read it without copying it.
const BASE="06 Sports Betting/Football/CFB", LEAGUE="cfb";
const norm=v=>String(v?.path??v??"").replace(/^\[\[|\]\]$/g,"").split("|")[0].replace(/\.md$/,"");
const link=v=>`[[${norm(v)}]]`, q=v=>JSON.stringify(v??"");
const safe=v=>{const s=String(v||"").trim().replace(/[\\/:*?"<>|#\[\]\r\n]/g,"-").replace(/[. ]+$/,"");if(!s)throw new Error("Enter a team name.");return s;};
function properties(raw,parseYaml){const m=raw.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);if(!m)throw new Error("Note needs Properties.");return {data:parseYaml(m[1]),header:m[0],body:raw.slice(m[0].length)};}
function sections(raw){
  const result={context:"",away:{offense:"",defense:"",context:""},home:{offense:"",defense:"",context:""}},buffers={};
  let side=null,key=null,fence=null;
  for(const line of raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/,"").split(/\r?\n/)){
    const f=line.match(/^\s*(`{3,}|~{3,})/);
    if(f){if(!fence)fence=f[1][0];else if(f[1][0]===fence)fence=null;if(key)(buffers[key]??=[]).push(line);continue;}
    if(!fence){
      if(/^## Game context\s*$/.test(line)){side=null;key="context";continue;}
      const team=line.match(/^## (Away|Home) team(?:\s*[—:-].*)?\s*$/i);
      if(team){side=team[1].toLowerCase();key=null;continue;}
      const category=line.match(/^### (Offense|Defense|Context)\s*$/i);
      if(category&&side){key=side+"."+category[1].toLowerCase();continue;}
      if(/^## /.test(line)){side=null;key=null;continue;}
      if(/^### /.test(line)){key=null;continue;}
    }
    if(key)(buffers[key]??=[]).push(line);
  }
  for(const [k,lines] of Object.entries(buffers)){
    const value=lines.join("\n").trim();if(k==="context")result.context=value;else{const [s,c]=k.split(".");result[s][c]=value;}
  }
  return result;
}
const hasNotes=s=>String(s||"").split("\n").some(l=>l.trim()&&!/^\s*[-*+]\s*$/.test(l));
function gameBody(game,sourcePath=""){
  const props={type:`${LEAGUE}-game`,season:game.season,week:game.week??"",game_date:String(game.game_date).slice(0,10),
    away_team:link(game.away_team),home_team:link(game.home_team),source_matchup:sourcePath?link(sourcePath):"",site_url:game.site_url||"",tape_url:""};
  const name=v=>norm(v).split("/").pop();
  let body=`---\n${Object.entries(props).map(([k,v])=>`${k}: ${q(v)}`).join("\n")}\n---\n\n# ${name(game.away_team)} at ${name(game.home_team)}\n\n${link(game.away_team)} · ${link(game.home_team)} · [[${BASE}/CFB Dashboard|Notebook]]\n\n## Game context\n\n`;
  if(game.venue)body+=`- Venue: ${game.venue}\n`;
  body+="\n";
  for(const side of ["away","home"]){body+=`## ${side==="away"?"Away":"Home"} team — ${name(game[`${side}_team`])}\n\n`;for(const category of ["Offense","Defense","Context"])body+=`### ${category}\n\n- \n\n`;}
  body+=`## Check against the site\n\n- \n\n\`\`\`dataviewjs\nawait dv.view("${BASE}/Views/cfb-film-view", {mode:"compare"});\n\`\`\`\n`;
  return body;
}
function teamBody(name){return `# ${name}\n\n[[${BASE}/CFB Dashboard|Game notebook]]\n\n\`\`\`dataviewjs\nawait dv.view("${BASE}/Views/cfb-film-view", {mode:"team"});\n\`\`\`\n`;}
function upgradeTeam(raw,oldBlueprint,parseYaml){
  const p=properties(raw,parseYaml),name=p.data.team_name||"Team";
  const stock=oldBlueprint.replaceAll("__TEAM_JSON__",q(name)).replaceAll("__TEAM_NAME__",name).replaceAll("__SEASON__",String(p.data.season));
  const old=properties(stock,parseYaml).body;
  let body=teamBody(name);
  if(p.body.trim()!==old.trim()&&!p.body.includes(`/Views/cfb-film-view`))body+=`\n> [!abstract]- Earlier team-page writing — preserved\n${p.body.trim().split("\n").map(l=>"> "+l).join("\n")}\n`;
  else if(p.body.includes(`/Views/cfb-film-view`))return raw;
  return p.header+"\n"+body;
}
async function folder(vault,path){let part="";for(const p of path.split("/")){part=part?part+"/"+p:p;if(!vault.getAbstractFileByPath(part))await vault.createFolder(part);}}
async function engineFor(tp){const f=tp.app.vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);if(!f)throw new Error("Engine helper has not synced.");return new Function(await tp.app.vault.read(f))();}
async function ensure(tp,sourcePath){
  const vault=tp.app.vault,source=vault.getAbstractFileByPath(sourcePath);if(!source)throw new Error("Imported game not found.");
  const game=properties(await vault.read(source),tp.obsidian.parseYaml).data;if(game.type!==`${LEAGUE}-matchup`)throw new Error("Choose a game from this league.");
  const candidates=[];
  for(const f of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Games/`))){
    const raw=await vault.read(f);if(!raw.startsWith("---"))continue;const p=properties(raw,tp.obsidian.parseYaml).data;if(p.type!==`${LEAGUE}-game`)continue;
    if(norm(p.source_matchup)===norm(sourcePath)||!p.source_matchup&&String(p.game_date).slice(0,10)===String(game.game_date).slice(0,10)&&norm(p.home_team)===norm(game.home_team)&&norm(p.away_team)===norm(game.away_team))candidates.push(f);
  }
  if(candidates.length>1)throw new Error("Multiple game notes match this source; resolve duplicates first.");
  // Re-opening or refreshing a watched game never replaces prose.
  if(candidates.length){
    const f=candidates[0],original=await vault.read(f),p=properties(original,tp.obsidian.parseYaml).data;
    if(!p.source_matchup){const e=await engineFor(tp);const next=e.patchProperties(original,{source_matchup:link(sourcePath),site_url:siteURL(game)},tp.obsidian.parseYaml);await vault.process(f,current=>{if(current!==original)throw new Error("Game notes changed; retry.");return next;});}
    return f.path;
  }
  await folder(vault,`${BASE}/Games`);
  const path=`${BASE}/Games/${String(game.game_date).slice(0,10)} - ${safe(norm(game.away_team).split("/").pop())} at ${safe(norm(game.home_team).split("/").pop())}.md`;
  if(vault.getAbstractFileByPath(path))throw new Error("Game-note filename already exists; existing writing was preserved.");
  await vault.create(path,gameBody({...game,site_url:siteURL(game)},sourcePath));return path;
}
function siteURL(game){try{const u=new URL(game.engine_api_url);if(u.protocol!=="https:"&&!['localhost','127.0.0.1'].includes(u.hostname))return "";return `${u.origin}/college-football/games/${encodeURIComponent(game.engine_game_id)}/`;}catch{return "";}}
async function manual(tp){
  const vault=tp.app.vault;
  const ask=async(label,fallback="")=>{const v=await tp.system.prompt(label,fallback,true);if(v===null)throw new Error("Cancelled.");return v.trim();};
  const date=await ask("Game date (YYYY-MM-DD)",tp.date.now("YYYY-MM-DD"));if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!Number.isFinite(Date.parse(date+"T12:00Z"))||new Date(date+"T12:00Z").toISOString().slice(0,10)!==date)throw new Error("Enter a valid game date.");
  const season=await ask("Season (year the season began)",date.slice(0,4));if(!/^\d{4}$/.test(season))throw new Error("Season must have four digits.");
  const teams=[];for(const f of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Teams/`))){const raw=await vault.read(f);if(!raw.startsWith("---"))continue;const p=properties(raw,tp.obsidian.parseYaml).data;if(p.type===`${LEAGUE}-team`)teams.push(f);}
  const choose=async label=>{const f=await tp.system.suggester([...teams.map(f=>f.basename),"+ New team"],[...teams,null],true,label);if(f)return f.path;const name=safe(await ask("Team name")),path=`${BASE}/Teams/${name}.md`;if(vault.getAbstractFileByPath(path))throw new Error("Team page already exists; select it from the list.");return {path,name};};
  const away=await choose("Away team"),home=await choose("Home team"),a=away.path||away,h=home.path||home;if(norm(a)===norm(h))throw new Error("Home and away must differ.");
  const target=`${BASE}/Games/${date} - ${safe(norm(a).split("/").pop())} at ${safe(norm(h).split("/").pop())}.md`;
  if(vault.getAbstractFileByPath(target))return target;
  for(const t of [away,home])if(typeof t==="object"){await folder(vault,`${BASE}/Teams`);await vault.create(t.path,`---\ntype: ${LEAGUE}-team\nteam_name: ${q(t.name)}\nseason: ${Number(season)}\n---\n\n`+teamBody(t.name));}
  await folder(vault,`${BASE}/Games`);await vault.create(target,gameBody({season:Number(season),game_date:date,away_team:a,home_team:h}));return target;
}
async function jsonRequest(tp,url){
  let timer;try{
    const r=await Promise.race([tp.obsidian.requestUrl({url,method:"GET",throw:false}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error("Schedule request timed out. Retry or use the game URL option.")),45000);})]);
    if(r.status!==200||!r.json||typeof r.json!=="object")throw new Error(`Could not load schedule data (HTTP ${r.status}). Retry or use the game URL option.`);return r.json;
  }finally{clearTimeout(timer);}
}
function scheduleGames(packet,team,season,base,engine,zone="America/New_York"){
  const nfl=BASE.endsWith("/NFL");
  if(String(packet.season)!==String(season)||String(nfl?packet.team?.abbreviation:packet.team?.team_id)!==String(team.id))throw new Error("The returned schedule belongs to another team or season.");
  const rows=nfl?packet.schedule?.rows:packet.schedule;if(!Array.isArray(rows))throw new Error("The engine did not return a team schedule.");
  const seen=new Set(),games=[];
  for(const row of rows){
    const home=nfl?row.home:row.home_team_id,away=nfl?row.away:row.away_team_id;
    if(![home,away].some(id=>String(id)===String(team.id)))throw new Error("A schedule row belongs to another team; no notes were changed.");
    const id=nfl?engine.parseGameInput(new URL(row.date_url,base).href,base):engine.parseGameInput(String(row.game_id),base);
    if(seen.has(String(id)))continue;seen.add(String(id));
    const isHome=String(home)===String(team.id),complete=nfl?!!row.result:!!row.completed;
    const own=nfl?(isHome?row.home_score:row.away_score):(isHome?row.home_points:row.away_points),other=nfl?(isHome?row.away_score:row.home_score):(isHome?row.away_points:row.home_points);
    const result=nfl?row.result:own!=null&&other!=null?(own>other?"W":own<other?"L":"T"):"";
    const score=nfl?row.score:own!=null&&other!=null?`${own}–${other}`:"";
    games.push({id,date:nfl?row.date:engine.dateInZone(row.start_date,zone),week:row.week,isHome,
      opponent:nfl?row.opponent:(isHome?row.away_team:row.home_team),complete,result,score});
  }
  return games.sort((a,b)=>String(a.date).localeCompare(String(b.date))||String(a.id).localeCompare(String(b.id)));
}
async function selectGame(tp){
  const engine=await engineFor(tp),vault=tp.app.vault,nfl=BASE.endsWith("/NFL");
  const configFile=vault.getAbstractFileByPath(`${BASE}/Views/engine-config.json`),config=configFile?JSON.parse(await vault.read(configFile)):{};
  const base=engine.baseUrl(config.api_url||"https://cfb-intelligence.onrender.com");
  const local=[];
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Teams/`))){const raw=await vault.read(file);if(!raw.startsWith("---"))continue;const p=properties(raw,tp.obsidian.parseYaml).data;if(p.type===`${LEAGUE}-team`&&p.engine_team_id!=null)local.push({id:p.engine_team_id,name:p.team_name||file.basename,season:p.season});}
  let teams;
  if(nfl&&local.length>=32)teams=local;
  else{const catalog=await jsonRequest(tp,`${base}/api/v1/${LEAGUE}${nfl?"":"/teams?limit=200"}`);teams=(catalog.teams||[]).map(t=>({id:nfl?t.abbreviation:t.team_id,name:nfl?t.name:t.school}));}
  const unique=new Map([...teams,...local].filter(t=>t.id!=null&&t.name).map(t=>[String(t.id),t]));
  const choices=[...unique.values()].sort((a,b)=>a.name.localeCompare(b.name));if(!choices.length)throw new Error("No teams returned by the engine. Use the game URL option instead.");
  const team=await tp.system.suggester([...choices.map(t=>t.name),...(nfl?[]:["Search another college team…"])],[...choices,...(nfl?[]:["search"])],true,"Choose a team (type to search)");if(team==null)return null;
  let selected=team;
  if(team==="search"){
    const query=await tp.system.prompt("College team name or alias","",true);if(query===null)return null;if(query.trim().length<2)throw new Error("Enter at least two letters.");
    const matches=await jsonRequest(tp,`${base}/api/v1/cfb/teams/resolve?q=${encodeURIComponent(query.trim())}`);
    const found=(matches.matches||[]).map(t=>({id:t.team_id,name:t.school}));if(!found.length)throw new Error("No matching team was found.");
    selected=await tp.system.suggester(found.map(t=>t.name),found,true,"Choose matching team");if(!selected)return null;
  }
  const year=new Date().getFullYear()-(nfl&&new Date().getMonth()<2?1:0);
  const season=await tp.system.prompt("Season (year the season began)",String(selected.season||year),true);if(season===null)return null;
  if(!/^\d{4}$/.test(season.trim()))throw new Error("Enter a four-digit season.");
  const packet=await jsonRequest(tp,`${base}/api/v1/${LEAGUE}/teams/${encodeURIComponent(selected.id)}?season=${season.trim()}`);
  const games=scheduleGames(packet,selected,season.trim(),base,engine,config.timezone||"America/New_York");
  if(!games.length)throw new Error(`No games are stored for ${selected.name} in ${season}. Choose another season or use a game URL.`);
  const names=new Map(choices.map(t=>[String(t.id),t.name]));
  const labels=games.map(g=>`${g.date} · W${g.week??"?"} · ${g.isHome?"vs":"at"} ${names.get(String(g.opponent))||g.opponent} · ${g.complete?`Final ${g.result||""} ${g.score||""}`.trim():"Scheduled"}`);
  const chosen=await tp.system.suggester(labels,games,true,`${selected.name} · ${season} · all games`);return chosen?String(chosen.id):null;
}
async function existingGame(tp,id){
  const vault=tp.app.vault,matches=[];
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Games/`))){
    const raw=await vault.read(file);if(!raw.startsWith("---"))continue;const p=properties(raw,tp.obsidian.parseYaml).data;
    if(p.type!==`${LEAGUE}-game`||!p.source_matchup)continue;
    const source=vault.getAbstractFileByPath(norm(p.source_matchup)+".md");if(!source)continue;
    const meta=properties(await vault.read(source),tp.obsidian.parseYaml).data;
    if(meta.type===`${LEAGUE}-matchup`&&String(meta.engine_game_id)===String(id))matches.push(file.path);
  }
  if(matches.length>1)throw new Error("Multiple game notes match this ID. Resolve duplicates before opening.");return matches[0]||null;
}

async function run(tp){
  const active=tp.file.path(true);if(active.startsWith(`${BASE}/Games/`)||active.startsWith(`${BASE}/Matchups/`))throw new Error("Run Game Notes Template from the dashboard so Templater cannot overwrite the game you are editing.");
  const action=await tp.system.suggester(["Select team → season → game","Game from site — past or current (URL/ID)","Open notes for an already imported game","Manual game — no import"],["select","import","existing","manual"],true,"Game notes");if(!action)return;
  let path;
  if(action==="manual")path=await manual(tp);
  else if(action==="existing"){
    const files=[];for(const f of tp.app.vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))){const raw=await tp.app.vault.read(f);if(!raw.startsWith("---"))continue;const p=properties(raw,tp.obsidian.parseYaml).data;if(p.type===`${LEAGUE}-matchup`)files.push(f);}
    const source=await tp.system.suggester(files.map(f=>f.basename),files,true,"Choose the game that supplied the research");if(!source)return;path=await ensure(tp,source.path);
  }else{
    const input=action==="select"?await selectGame(tp):await tp.system.prompt("Paste a past or current game URL/ID from the site.","",true);if(input===null)return;if(!input.trim())throw new Error("Paste the specific game URL/ID so the notes refer to the game you watched.");
    if(action==="select"){const existing=await existingGame(tp,input);if(existing){tp.hooks.on_all_templates_executed(()=>tp.app.workspace.openLinkText(existing,"",false));return existing;}}
    const engine=await engineFor(tp);
    // Use the proven importer for source data; open only the simple game note.
    const system={...tp.system,prompt:async(label,...args)=>label.startsWith("Paste engine game")?input:tp.system.prompt(label,...args),suggester:async(labels,values,required,title)=>/^Choose sportsbook|^Choose supplied market/.test(title||"")?values[0]:tp.system.suggester(labels,values,required,title)};
    const source=await engine.run({...tp,system,hooks:{...tp.hooks,on_all_templates_executed(){}}});path=await ensure(tp,source);
  }
  tp.hooks.on_all_templates_executed(()=>tp.app.workspace.openLinkText(path,"",false));return path;
}
return {norm,properties,sections,hasNotes,gameBody,teamBody,upgradeTeam,ensure,manual,run,siteURL,selectGame,scheduleGames,existingGame};
