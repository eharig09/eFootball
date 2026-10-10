// Local Templater integration. Fetches JSON from the configured engine; never sends vault notes.
const BASE = "06 Sports Betting/Football/CFB";
const DEFAULT_URL = "https://cfb-intelligence.onrender.com";
const CFG = `${BASE}/Views/engine-config.json`;
const clone = value => JSON.parse(JSON.stringify(value));
const scalar = value => value == null ? "" : JSON.stringify(value);
const nameKey = value => String(value ?? "").normalize("NFKC").trim().toLowerCase();
const linkPath = value => String(value?.path ?? value ?? "").replace(/^\[\[/, "").replace(/\]\]$/, "")
  .split("|")[0].replace(/\.md$/, "");
const link = path => `[[${path.replace(/\.md$/, "")}]]`;
const safeName = value => {
  const result = String(value ?? "").normalize("NFKC").replace(/[\\/:*?"<>|#\[\]\r\n]/g, "-").trim().replace(/[. ]+$/, "");
  if (!result || result === "." || result === "..") throw new Error("Engine returned an invalid team name.");
  return result;
};
const cell = value => String(value ?? "").replace(/<!--/g, "&lt;!--").replace(/<%/g,"&lt;%")
  .replace(/\r?\n/g, "<br>").replace(/\|/g, "\\|");
function sourceLink(url, label = "source") {
  try {
    const parsed = new URL(url);
    if (!["http:","https:"].includes(parsed.protocol)) return "source URL unavailable";
    const target = parsed.href.replace(/[<>\s]/g,c=>encodeURIComponent(c));
    return `[${cell(label).replace(/[\[\]]/g, "") }](<${target}>)`;
  } catch { return "source URL unavailable"; }
}
function dateInZone(value, zone = "America/New_York") {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error("Engine game has no valid kickoff date.");
  const parts = new Intl.DateTimeFormat("en-US", {timeZone:zone,year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(date);
  const get = type => parts.find(p=>p.type===type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}
function baseUrl(value) {
  const url = new URL(value);
  if (!(["https:"].includes(url.protocol) || url.protocol === "http:" && ["localhost","127.0.0.1","[::1]"].includes(url.hostname)))
    throw new Error("Use an HTTPS engine address, or HTTP on localhost.");
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/")
    throw new Error("Use the engine's root address, without credentials, path, query, or fragment.");
  return url.origin;
}
function parseGameInput(value, base) {
  if (/^\d+$/.test(String(value).trim()) && Number(value)>0) return Number(value);
  let url;
  try { url = new URL(String(value).trim()); } catch { throw new Error("Enter a numeric game ID or an engine game-page URL."); }
  if (url.origin !== base) throw new Error("Game link belongs to a different engine address. Change engine-config.json first.");
  const match = url.pathname.match(/^\/(?:college-football|api\/v1\/cfb)\/games\/(\d+)(?:\/|$)/);
  if (!match) throw new Error("That URL is not a CFB game page.");
  return Number(match[1]);
}
function frontmatter(text, parseYaml) {
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) throw new Error("Matchup/team note needs YAML Properties at the top.");
  const data = parseYaml(match[1]);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid note Properties.");
  return {data,match};
}
function patchProperties(text, updates, parseYaml, fillOnly = {}) {
  const {data,match} = frontmatter(text, parseYaml);
  let yaml = match[1];
  const set = (key,value) => {
    // Only our generated scalar keys and blank template fields are written.
    const regex = new RegExp(`^${key}:[^\\r\\n]*(?:\\r?\\n[ \\t]+[^\\r\\n]*)*`,"m");
    const line = `${key}: ${scalar(value)}`;
    yaml = regex.test(yaml) ? yaml.replace(regex,()=>line) : `${yaml}\n${line}`;
  };
  for (const [key,value] of Object.entries(updates)) set(key,value);
  for (const [key,value] of Object.entries(fillOnly))
    if ((data[key] == null || data[key] === "") && value != null) set(key,value);
  return `---\n${yaml}\n---\n${text.slice(match[0].length)}`;
}
function block(text, id) {
  const start = `<!-- cfb:${id}:start -->`, end = `<!-- cfb:${id}:end -->`;
  const a = text.indexOf(start), b = text.indexOf(end);
  if (a<0 && b<0) return null;
  if(a<0 || b<a || text.indexOf(start,a+start.length)>=0 || text.indexOf(end,b+end.length)>=0)
    throw new Error(`Damaged or duplicate import markers: ${id}. Restore both markers before refreshing.`);
  return {a,b,end:b+end.length,content:text.slice(a+start.length,b).trim()};
}
function replaceBlock(text,id,content) {
  const existing = block(text,id);
  const replacement = `<!-- cfb:${id}:start -->\n${content}\n<!-- cfb:${id}:end -->`;
  return existing ? text.slice(0,existing.a)+replacement+text.slice(existing.end) : `${text.trimEnd()}\n\n${replacement}\n`;
}
function splitCells(line) {
  const output=[]; let value="",wiki=0;
  line=line.trim().replace(/^\|/,"").replace(/\|$/,"");
  for(let i=0;i<line.length;i++) {
    if(line[i]==="\\"&&line[i+1]==="|"){value+="|";i++;continue;}
    if(line.slice(i,i+2)==="[["){wiki++;value+="[[";i++;continue;}
    if(line.slice(i,i+2)==="]]"){wiki=Math.max(0,wiki-1);value+="]]";i++;continue;}
    if(line[i]==="|"&&!wiki){output.push(value.trim());value="";}else value+=line[i];
  }
  output.push(value.trim()); return output;
}
function mergeRows(old, fresh) {
  const stored = new Map();
  for(const line of String(old ?? "").split(/\r?\n/).filter(x=>x.trim().startsWith("|"))) {
    const c=splitCells(line);
    if(c.length===6 && c[0]!=="+" && !/^:?-+:?$/.test(c[0]) && c[5])
      stored.set(c[5],{plus:c[0],minus:c[1],evidence:c[2],carry:c[3],review:c[4],id:c[5]});
  }
  for(const row of fresh) {
    const previous=stored.get(row.id);
    stored.set(row.id, previous ? {...row,plus:previous.plus,minus:previous.minus,carry:previous.carry,review:previous.review} : row);
  }
  const lines=["| + | - | Evidence / condition | Carry forward | Review / expires | Item ID |","| --- | --- | --- | --- | --- | --- |"];
  for(const r of stored.values()) lines.push(`| ${[r.plus,r.minus,r.evidence,r.carry,r.review,r.id].map(cell).join(" | ")} |`);
  return lines.join("\n");
}
function mergeReporting(old,fresh) {
  const records=new Map();
  for(const text of [old||"",fresh]) for(const line of text.split(/\r?\n/).filter(x=>x.trim().startsWith("|"))) {
    const c=splitCells(line);
    if(c.length===5 && c[0]!=="Item" && !/^:?-+:?$/.test(c[0]) && c[4]) records.set(c[4],c);
  }
  const head=fresh.split("| Item |",1)[0].trimEnd();
  return head+"\n\nPreviously imported items are retained by ID. Review publication dates for relevance.\n\n"+
    "| Item | Role / layer | Published | Source | ID |\n| --- | --- | --- | --- | --- |\n"+
    [...records.values()].map(c=>`| ${c.map(cell).join(" | ")} |`).join("\n");
}
function fact(id,evidence,review="",carry="game-only") {return {id,plus:"",minus:"",evidence,carry,review};}
function gameValid(game,id) {
  if (!game || Number(game.game_id)!==id || !game.home_team || !game.away_team ||
      !Number.isInteger(Number(game.season)) || !Number.isInteger(Number(game.home_team_id)) ||
      !Number.isInteger(Number(game.away_team_id)) || Number(game.home_team_id)===Number(game.away_team_id))
    throw new Error("Engine returned an invalid or mismatched game packet.");
  dateInZone(game.start_date);
}
function buildImported(packet, provider, importedAt, base, zone = "America/New_York") {
  const {game, preview, situation, content, errors = []} = packet;
  const source = `${base}/college-football/games/${game.game_id}/`;
  const imported = {};
  let context = ["## Import status", "", `Fetched: ${importedAt}. ${sourceLink(source,"Engine game")}`,
    "", "These are sourced facts and engine descriptions. Assess their direction in your own + / - tables."];
  if (Date.parse(game.start_date)<=Date.parse(importedAt) || game.completed)
    context.push("", "**Retrospective import:** fetched after kickoff; this is not a pregame snapshot.");
  if(game.notes) context.push("",cell(game.notes));
  if(errors.length) context.push("",`**Partial import:** ${errors.join("; ")}. Existing sections for unavailable endpoints were retained.`);
  imported["engine-import-status"]=context.join("\n");
  if(situation) {
    context=["## Imported game context","",`Fetched: ${importedAt}. ${sourceLink(source,"Engine game")}`];
    const weather=situation.weather;
    if(weather?.available) {
      const w=weather.latest||{};
      const vals=[w.condition, w.temperature!=null?`${w.temperature} °F`:null,
        w.sustained_wind!=null?`${w.sustained_wind} mph wind`:null,w.wind_gust!=null?`${w.wind_gust} mph gusts`:null,
        w.precipitation_probability!=null?`${w.precipitation_probability}% precipitation probability`:null].filter(Boolean);
      context.push("",`**Weather:** ${vals.join(" · ")}${weather.indoor?" · indoor venue":""}. Forecast issued ${w.forecast_generated_at||"time unavailable"}; source ${w.source||"unspecified"}.`);
    } else context.push("", "**Weather:** no forecast available.");
    if(situation.travel?.detail && !game.neutral_site) context.push("",`**Travel:** ${cell(situation.travel.detail)}`);
    if(game.neutral_site) context.push("", "**Neutral site:** engine home-campus travel assumptions were not imported.");
    for(const spot of situation.schedule_spots||[]) context.push("",`- ${cell(spot.team)}: ${cell(spot.headline)} — ${cell(spot.detail)} (schedule-derived, not a prediction)`);
    if(provider) context.push("", `**Latest market:** ${cell(provider.provider)} · home spread ${provider.spread ?? "unavailable"} · total ${provider.over_under ?? "unavailable"} · quote fetched ${provider.fetched_at||"time unavailable"}. Opening home spread ${provider.spread_open ?? "unavailable"}; opening total ${provider.over_under_open ?? "unavailable"}. Spread/total juice unavailable.`);
    else context.push("", "**Market:** no selected provider quote available; no consensus quote substituted.");
    imported["engine-game-context"]=context.join("\n");
  }
  for(const side of ["home","away"]) {
    const metrics=preview?.[`${side}_quality`]?.metrics?.advanced || game.advanced_metrics?.[game[`${side}_team`]];
    for(const category of ["offense","defense"]) {
      const items=[];
      const definitions=[["success_rate","Success rate"],["ppa","PPA / play"],["explosiveness","Explosiveness"],["havoc","Havoc"]];
      if(metrics) for(const [key,label] of definitions) {
        const value=metrics[`${category}_${key}`];
        if(value!=null) items.push(fact(`metric:${side}:${category}:${key}`,`${label}: ${key==="success_rate"||key==="havoc"?(100*Number(value)).toFixed(1)+"%":value}. ${metrics.season??game.season} season aggregate; not an opponent-adjusted betting edge. Imported ${importedAt}. ${sourceLink(source,"engine metrics")}`));
      }
      if(preview?.projection) {
        const values=preview.projection[`${side}_snapshot`];
        for(const [key,label] of [["seconds_per_play","Seconds / play"],["pass_rate","Pass rate"],["yards_per_dropback","Yards / dropback"],["yards_per_rush","Yards / rush"]]) {
          const field=category==="offense"?key:`${key}_allowed`;
          if(values?.[field]!=null) items.push(fact(`pace:${side}:${category}:${key}`,`${label}${category==="defense"?" allowed":""}: ${key==="pass_rate"?(100*Number(values[field])).toFixed(1)+"%":Number(values[field]).toFixed(2)}. Trailing snapshot as of ${preview.projection.as_of_date||"unspecified"}; sample ${values.games??"unspecified"}. ${sourceLink(source,"engine projection inputs")}`));
        }
      }
      if(items.length) imported[`engine-${side}-${category}`]=items;
      else if(preview) imported[`engine-${side}-${category}`]=[];
    }
    if(situation) {
      const rows=[]; const review=dateInZone(game.start_date,zone);
      for(const spot of situation.schedule_spots||[]) if(nameKey(spot.team)===nameKey(game[`${side}_team`]))
        rows.push(fact(`spot:${side}:${spot.type}`,`${spot.headline}: ${spot.detail}. Schedule-derived context, not a prediction. ${sourceLink(source,"engine situation")}`,review));
      for(const item of situation.availability||[]) if(nameKey(item.school)===nameKey(game[`${side}_team`])) {
        const published=item.published_at||"publication time unavailable";
        // Headlines are reported availability, never automatically confirmed absences or directional picks.
        rows.push(fact(`availability:${side}:${item.content_id??item.canonical_url}`,`${item.title||item.headline||"Availability report"}. Role: ${item.source_role||"unspecified"}. Published ${published}. ${sourceLink(item.canonical_url,item.publisher_name||item.source_name||"original source")}`,review,"watch"));
      }
      imported[`engine-${side}-context`]=rows;
    }
  }
  if(content) {
    const seen=new Set(), lines=["## Imported reporting", "", `Fetched: ${importedAt}. Team-context items may not discuss this specific matchup; original labels are preserved.`, "", "| Item | Role / layer | Published | Source | ID |", "| --- | --- | --- | --- | --- |"];
    for(const [layer,items] of Object.entries(content)) for(const item of Array.isArray(items)?items:[]) {
      const id=String(item.content_id??item.canonical_url??""); if(!id||seen.has(id))continue; seen.add(id);
      lines.push(`| ${cell(item.title||item.headline||"Untitled")} | ${cell(item.source_role||layer)} · ${cell(item.relevance_label||"context")} | ${cell(item.published_at||"unknown")} | ${sourceLink(item.canonical_url,item.publisher_name||item.source_entity_name||"original source")} | ${cell(id)} |`);
    }
    if(!seen.size) lines.push("", "No reporting returned for this game.");
    imported["engine-reporting"]=lines.join("\n");
  }
  if(preview?.projection) {
    const p=preview.projection;
    const lines=["## Imported projection", "", `Inputs as of ${p.as_of_date||"unspecified"}; fetched ${importedAt}. ${p.insufficient_data?"**Insufficient data flagged by engine.**":""}`,
      "", "| Team | Projected drives | Projected plays | Projected points |", "| --- | --- | --- | --- |"];
    for(const side of ["away","home"]) {
      const s=p[side]||{}; lines.push(`| ${cell(game[`${side}_team`])} | ${s.drives??"—"} | ${s.plays??"—"} | ${s.expected_points??"—"} |`);
    }
    lines.push("", `Points model: ${p.points_model?.model_version||"not supplied"}. These are projections, not quoted market prices.`);
    imported["engine-projection"]=lines.join("\n");
  }
  return imported;
}
function emptyNote(game,teamPaths,day) {
  const props={type:"cfb-matchup",season:game.season,week:String(game.week),game_date:day,
    home_team:link(teamPaths.home),away_team:link(teamPaths.away),line:null,total:null,price:null,
    price_market:"",price_selection:"",home_spread_price:null,away_spread_price:null,over_price:null,under_price:null,
    home_moneyline:null,away_moneyline:null,sportsbook:"",market_timestamp:"",neutral_site:!!game.neutral_site,
    venue:game.venue||"",kickoff:game.start_date,game_context:"",status:"research",next_action:"",review_date:null,
    home_score:null,away_score:null};
  let text=`---\n${Object.entries(props).map(([k,v])=>`${k}: ${scalar(v)}`).join("\n")}\ntags:\n  - football/cfb/matchup\n---\n\n# ${game.away_team} at ${game.home_team}\n\n`;
  text+=`**Away:** ${link(teamPaths.away)} · **Home:** ${link(teamPaths.home)}\n\n[[${BASE}/CFB Dashboard|CFB Dashboard]]\n\n`;
  text+="> line = HOME spread. price = American odds for price_market + price_selection. Initial quoted fields are preserved on refresh; engine_* fields and imported market context show the latest quote.\n\n";
  text+="## Game context\n\n<!-- cfb:game-context:start -->\n- Weather / surface:\n- Rest / travel / scheduling:\n- Stakes / rivalry:\n- Game script:\n<!-- cfb:game-context:end -->\n\n";
  for(const side of ["away","home"]) {
    text+=`## ${side==="home"?"Home":"Away"} team\n\n${link(teamPaths[side])}\n\n`;
    for(const cat of ["Offense","Defense","Context"])
      text+=`### ${cat}\n\n<!-- cfb:${side}-${cat.toLowerCase()}:start -->\n| + | - | Evidence / condition | Carry forward | Review / expires |\n| --- | --- | --- | --- | --- |\n| | | | game-only | |\n<!-- cfb:${side}-${cat.toLowerCase()}:end -->\n\n`;
  }
  return text+"## Decision and next action\n\n- Thesis:\n- Conflicting evidence:\n- Price / information needed:\n- Decision:\n- Pregame conclusion and timestamp:\n\n## Postgame review\n\n- What happened:\n- What held up / changed:\n- Carry-forward rows updated:\n\n## Sources\n\n- \n\n## Engine imports\n\nOnly the marked imported blocks below are refreshed. Your sections above remain editable.\n";
}
function refresh(text,packet,provider,teamPaths,importedAt,base,parseYaml,zone) {
  const game=packet.game;
  const fm=frontmatter(text,parseYaml).data;
  if(fm.type!=="cfb-matchup")throw new Error("Target is not a CFB matchup note.");
  if(fm.engine_game_id && Number(fm.engine_game_id)!==Number(game.game_id))throw new Error("Target belongs to another engine game.");
  for(const side of ["home","away"]) if(linkPath(fm[`${side}_team`])!==linkPath(teamPaths[side]))
    throw new Error(`Target ${side} team does not match the engine mapping. Fix the team link before importing.`);
  if(String(fm.season)!==String(game.season))throw new Error("Target season does not match the engine game.");
  const updates={engine_game_id:game.game_id,engine_home_team_id:game.home_team_id,engine_away_team_id:game.away_team_id,
    engine_api_url:base,engine_imported_at:importedAt,engine_kickoff:game.start_date,engine_completed:!!game.completed,
    engine_import_status:packet.errors?.length?"partial":"complete",engine_import_errors:(packet.errors||[]).join("; ")};
  if(packet.situation) Object.assign(updates,{engine_sportsbook:provider?.provider??null,engine_line:provider?.spread??null,
    engine_total:provider?.over_under??null,engine_spread_open:provider?.spread_open??null,engine_total_open:provider?.over_under_open??null,
    engine_home_moneyline:provider?.home_moneyline??null,engine_away_moneyline:provider?.away_moneyline??null,
    engine_market_timestamp:provider?.fetched_at??null});
  const initialMarket=provider && (!fm.sportsbook || nameKey(fm.sportsbook)===nameKey(provider.provider)) ?
    {line:provider.spread,total:provider.over_under,home_moneyline:provider.home_moneyline,away_moneyline:provider.away_moneyline,
      sportsbook:provider.provider,market_timestamp:provider.fetched_at} : {};
  text=patchProperties(text,updates,parseYaml,{...initialMarket,venue:game.venue,kickoff:game.start_date,
    home_score:game.completed?game.home_points:null,away_score:game.completed?game.away_points:null});
  for(const [id,value] of Object.entries(buildImported(packet,provider,importedAt,base,zone))) {
    const merged=Array.isArray(value)?mergeRows(block(text,id)?.content,value):
      id==="engine-reporting"?mergeReporting(block(text,id)?.content,value):value;
    text=replaceBlock(text,id,merged);
  }
  return text;
}
async function ensureFolder(vault,path) {
  let current="";
  for(const part of path.split("/")) { current=current?`${current}/${part}`:part;
    if(!vault.getAbstractFileByPath(current)) await vault.createFolder(current); }
}
async function configuration(tp) {
  const file=tp.app.vault.getAbstractFileByPath(CFG);
  const config=file?JSON.parse(await tp.app.vault.read(file)):{api_url:DEFAULT_URL,timezone:"America/New_York",preferred_sportsbook:""};
  config.api_url=baseUrl(config.api_url);dateInZone(new Date().toISOString(),config.timezone);
  return config;
}
function prohibitActiveMatchup(tp) {
  if(tp.file.path(true).startsWith(`${BASE}/Matchups/`))
    throw new Error("Run Engine Import or Freeze from the CFB Dashboard, not from a matchup being edited.");
}
async function request(tp,url) {
  let timer;
  let response;
  try {
    response=await Promise.race([tp.obsidian.requestUrl({url,method:"GET",throw:false}),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error("Request timed out")),45000);})]);
  } finally { clearTimeout(timer); }
  if(response.status!==200)throw new Error(`HTTP ${response.status}`);
  if(!response.json || typeof response.json!=="object")throw new Error("Expected JSON");
  return response.json;
}
async function teamPath(tp,game,side,parseYaml) {
  const vault=tp.app.vault, id=game[`${side}_team_id`], name=game[`${side}_team`];
  const files=vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Teams/`));
  const pages=[];
  for(const f of files) {
    const raw=await vault.read(f);
    if(!raw.startsWith("---"))continue;
    const props=frontmatter(raw,parseYaml).data;
    if(props.type==="cfb-team")pages.push({f,props});
  }
  let matches=pages.filter(x=>Number(x.props.engine_team_id)===Number(id));
  if(!matches.length)matches=pages.filter(x=>!x.props.engine_team_id && nameKey(x.props.team_name||x.f.basename)===nameKey(name));
  if(matches.length>1)throw new Error(`Multiple team pages match ${name}. Resolve duplicate pages first.`);
  let file=matches[0]?.f;
  if(!file && pages.length) {
    const eligible=pages.filter(x=>!x.props.engine_team_id);
    const selected=await tp.system.suggester([`Create ${name}`, ...eligible.map(x=>`Use existing: ${x.f.basename}`)],
      [null,...eligible.map(x=>x.f)],true,`Choose team page for ${name}`);
    if(selected)file=selected;
  }
  if(file) {
    const original=await vault.read(file);
    const next=patchProperties(original,{engine_team_id:id},parseYaml);
    if(original!==next)await vault.process(file,current=>{
      if(current!==original)throw new Error("Team note changed during import. Retry after finishing edits."); return next;
    });
    return file.path;
  }
  const target=`${BASE}/Teams/${safeName(name)}.md`;
  if(vault.getAbstractFileByPath(target))throw new Error(`Team filename collision: ${target}`);
  const blueprint=vault.getAbstractFileByPath(`${BASE}/Views/team-page.md`);
  if(!blueprint)throw new Error("Team blueprint has not synced yet.");
  let body=(await vault.read(blueprint)).replaceAll("__TEAM_JSON__",scalar(name)).replaceAll("__TEAM_NAME__",safeName(name)).replaceAll("__SEASON__",String(game.season));
  body=patchProperties(body,{engine_team_id:id},parseYaml,{conference:game[`${side}_conference`]});
  await ensureFolder(vault,`${BASE}/Teams`); await vault.create(target,body); return target;
}
async function run(tp) {
  prohibitActiveMatchup(tp);
  const config=await configuration(tp), base=config.api_url, parseYaml=tp.obsidian.parseYaml;
  let input=await tp.system.prompt("Paste engine game URL or ID. Leave blank to select an upcoming game.","",true);
  if(input===null)throw new Error("Cancelled.");
  let id;
  if(input.trim()) id=parseGameInput(input,base);
  else {
    const payload=await request(tp,`${base}/api/v1/cfb/games`);
    const games=payload.games||[]; if(!games.length)throw new Error("No upcoming games returned. Paste a game URL or ID instead.");
    const selected=await tp.system.suggester(games.map(g=>`${dateInZone(g.start_date,config.timezone)} · ${g.away_team} at ${g.home_team}`),games,true,"Choose a game");
    if(!selected)throw new Error("Cancelled."); id=Number(selected.game_id);
  }
  const game=await request(tp,`${base}/api/v1/cfb/games/${id}`); gameValid(game,id);
  const endpoints=["preview","situation","content"];
  const responses=await Promise.allSettled(endpoints.map(kind=>request(tp,`${base}/api/v1/cfb/games/${id}/${kind}`)));
  const packet={game,errors:[]};
  responses.forEach((result,index)=>{
    if(result.status==="fulfilled")packet[endpoints[index]]=result.value;
    else packet.errors.push(`${endpoints[index]} unavailable`);
  });
  if(packet.preview?.game && Number(packet.preview.game.game_id)!==id)throw new Error("Preview packet belongs to a different game.");
  if(packet.situation?.game_id && Number(packet.situation.game_id)!==id)throw new Error("Situation packet belongs to a different game.");
  const providers=(packet.situation?.lines?.providers||[]).filter(p=>p.provider);
  let provider=null;
  if(providers.length) {
    const preferred=providers.find(p=>nameKey(p.provider)===nameKey(config.preferred_sportsbook));
    const ordered=preferred?[preferred,...providers.filter(p=>p!==preferred)]:providers;
    provider=await tp.system.suggester(ordered.map(p=>`${p.provider}: home ${p.spread??"—"}, total ${p.over_under??"—"}`),ordered,true,"Choose sportsbook (never substitutes a consensus)");
    if(!provider)throw new Error("Cancelled.");
  }
  // Required requests and sportsbook selection finish before any note writes.
  const teamPaths={away:await teamPath(tp,game,"away",parseYaml),home:await teamPath(tp,game,"home",parseYaml)};
  const vault=tp.app.vault, candidates=[];
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))) {
    const text=await vault.read(file);
    if(!text.startsWith("---"))continue;
    const props=frontmatter(text,parseYaml).data;
    if(props.type!=="cfb-matchup")continue;
    const exact=Number(props.engine_game_id)===id;
    const manual=!props.engine_game_id && String(props.season)===String(game.season) && String(props.week)===String(game.week)
      && linkPath(props.home_team)===linkPath(teamPaths.home) && linkPath(props.away_team)===linkPath(teamPaths.away);
    if(exact||manual)candidates.push({file,text});
  }
  if(candidates.length>1)throw new Error("Multiple matchup notes match this game. Resolve duplicates before importing.");
  const now=new Date().toISOString(), day=dateInZone(game.start_date,config.timezone);
  const target=candidates[0]?.file.path??`${BASE}/Matchups/${day} - ${safeName(game.away_team)} at ${safeName(game.home_team)}.md`;
  const initial=candidates[0]?.text??emptyNote(game,teamPaths,day);
  const next=refresh(initial,packet,provider,teamPaths,now,base,parseYaml,config.timezone);
  await ensureFolder(vault,`${BASE}/Matchups`);
  if(candidates.length)await vault.process(candidates[0].file,current=>{
    if(current!==initial)throw new Error("Matchup changed during import. Retry after finishing edits."); return next;
  });
  else {if(vault.getAbstractFileByPath(target))throw new Error("Matchup filename already exists but did not match the game. Review it first.");await vault.create(target,next);}
  new tp.obsidian.Notice(`${candidates.length?"Refreshed":"Imported"}: ${game.away_team} at ${game.home_team}${packet.errors.length?" (partial import; see note)":""}`,8000);
  // Open only after Templater finishes updating the dashboard/editor.
  tp.hooks.on_all_templates_executed(()=>tp.app.workspace.openLinkText(target,"",false));
  return target;
}
async function freeze(tp) {
  prohibitActiveMatchup(tp);
  const vault=tp.app.vault, files=[];
  for(const f of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))) {
    const raw=await vault.read(f);
    if(raw.startsWith("---") && frontmatter(raw,tp.obsidian.parseYaml).data.type==="cfb-matchup")files.push(f);
  }
  if(!files.length)throw new Error("No matchup notes to snapshot.");
  const file=await tp.system.suggester(files.map(f=>f.basename),files,true,"Select matchup to freeze before kickoff");
  if(!file)throw new Error("Cancelled.");
  const text=await vault.read(file), props=frontmatter(text,tp.obsidian.parseYaml).data;
  const kickoff=Date.parse(props.engine_kickoff||props.kickoff), now=new Date();
  if(props.type!=="cfb-matchup" || !Number.isFinite(kickoff))throw new Error("Import this matchup first so its kickoff is known.");
  if(props.engine_completed || kickoff<=now.getTime())throw new Error("Kickoff has passed; a pregame snapshot cannot be created now.");
  const stamp=now.toISOString(), target=`${BASE}/Snapshots/${file.basename} - ${stamp.replace(/[:.]/g,"-")}.md`;
  const snapshot=patchProperties(text,{type:"cfb-pregame-snapshot",snapshot_at:stamp,source_matchup:link(file.path)},tp.obsidian.parseYaml);
  await ensureFolder(vault,`${BASE}/Snapshots`);
  if(vault.getAbstractFileByPath(target))throw new Error("Snapshot already exists. Retry in a moment.");
  await vault.create(target,snapshot);
  new tp.obsidian.Notice("Saved pregame snapshot, including your analysis and imported evidence.",8000);
  return target;
}
return {run,freeze,refresh,buildImported,emptyNote,mergeRows,patchProperties,parseGameInput,dateInZone,baseUrl,gameValid};
