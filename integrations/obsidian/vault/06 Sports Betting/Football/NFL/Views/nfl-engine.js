// Local Templater integration. Fetches JSON from the configured engine; never sends vault notes.
const BASE = "06 Sports Betting/Football/NFL";
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
function parseGameInput(value,base){
  const input=String(value).trim();if(/^\d{4}_\d{2}_[A-Z0-9]{2,3}_[A-Z0-9]{2,3}$/.test(input))return input;
  let url;try{url=new URL(input);}catch{throw new Error("Enter an NFL game ID such as 2026_05_CHI_GB, or an engine game URL.");}
  if(url.origin!==base)throw new Error("Game link belongs to a different engine address. Change engine-config.json first.");
  const match=url.pathname.match(/^\/(?:nfl|api\/v1\/nfl)\/games\/(\d{4}_\d{2}_[A-Z0-9]{2,3}_[A-Z0-9]{2,3})(?:\/|$)/);
  if(!match)throw new Error("That URL is not an NFL game page.");return match[1];
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
  const start = `<!-- nfl:${id}:start -->`, end = `<!-- nfl:${id}:end -->`;
  const a = text.indexOf(start), b = text.indexOf(end);
  if (a<0 && b<0) return null;
  if(a<0 || b<a || text.indexOf(start,a+start.length)>=0 || text.indexOf(end,b+end.length)>=0)
    throw new Error(`Damaged or duplicate import markers: ${id}. Restore both markers before refreshing.`);
  return {a,b,end:b+end.length,content:unfold(text.slice(a+start.length,b).trim())};
}
function replaceBlock(text,id,content) {
  const existing = block(text,id);
  const replacement = `<!-- nfl:${id}:start -->\n${content}\n<!-- nfl:${id}:end -->`;
  return existing ? text.slice(0,existing.a)+replacement+text.slice(existing.end) : `${text.trimEnd()}\n\n${replacement}\n`;
}
function unfold(content) {
  if (!content.startsWith("> [!abstract]-")) return content;
  return content.split(/\r?\n/).slice(1).map(line=>line.replace(/^> ?/, "")).join("\n").trim();
}
function folded(title, content) {
  return `> [!abstract]- ${title}\n${content.split("\n").map(line=>"> "+line).join("\n")}`;
}
function observationView(side, category) {
  return `\`\`\`dataviewjs\nawait dv.view("${BASE}/Views/nfl-observation-view", {side: "${side}", category: "${category}"});\n\`\`\``;
}
// Add workflow scaffolding once; never replace authored analysis or editorial fields.
function workflow(text,parseYaml){
  const data=frontmatter(text,parseYaml).data,defaults={article_status:"idea",reader_question:"",central_argument:"",publish_by:"",published_url:""};
  const missing=Object.fromEntries(Object.entries(defaults).filter(([k])=>!Object.hasOwn(data,k)));
  if(Object.keys(missing).length)text=patchProperties(text,missing,parseYaml);
  const sections=[
    ["decision-brief","## Decision brief\n\n- Thesis / expected game script:\n- Strongest evidence (link observations):\n- Counterargument:\n- Unresolved question:\n- Bet only if (selection, line, price, and information conditions):\n- Pass / invalidate if:\n- Decision and next check:\n"],
    ["prediction-review","## Predictions and review\n\nAdd a testable prediction to an observation before kickoff; record the actual outcome and result afterward. Freeze the pregame note before updating it.\n\n```dataviewjs\nawait dv.view(\"06 Sports Betting/Football/NFL/Views/nfl-observation-view\", {predictions:true});\n```\n\n### Postgame mechanism check\n\n- Expected vs. actual pace / scoring / pressure / personnel:\n- Did the mechanism hold, independent of the bet result?\n- What evidence contradicted the thesis?\n- What carries forward, and under which conditions?\n- Process change for next week:\n"],
    ["article-draft","> [!abstract]- Blog draft — write in this note\n> ### Pregame: the matchup question\n> Reader question → central argument → 2–3 linked observations → counterargument → market conditions / pass threshold.\n>\n> ### Postgame: what the game taught us\n> Original prediction → actual outcome → mechanism review → next matchup implication.\n>\n> Draft here in normal prose. Verify sources, quote the selection/line/price/book and an as-of date, and link the frozen pregame analysis before publication.\n"]
  ];
  for(const [id,body] of sections)if(!block(text,id)){
    const chunk=`\n<!-- nfl:${id}:start -->\n${body}<!-- nfl:${id}:end -->\n`;
    const pos=text.indexOf("## Decision and next action");text=pos<0?text+chunk:text.slice(0,pos)+chunk+"\n"+text.slice(pos);
  }
  return text;
}
function organize(text, parseYaml) {
  text=workflow(text,parseYaml);
  const props=frontmatter(text,parseYaml).data;
  const day=props.game_date instanceof Date?props.game_date.toISOString().slice(0,10):String(props.game_date).slice(0,10);
  const raw={};
  for(const match of text.matchAll(/<!-- nfl:(engine-[\w-]+):start -->/g))
    if(match[1]!=="engine-brief")raw[match[1]]=block(text,match[1]).content;
  const metric=(side,cat,key)=>{
    const line=(raw[`engine-${side}-${cat}`]||"").split("\n").find(line=>line.includes(`metric:${side}:${cat}:${key} `)||line.includes(`pace:${side}:${cat}:${key} `));
    if(!line)return "—";
    const evidence=splitCells(line)[2];return evidence?.match(/: (-?\d+(?:\.\d+)?%?)(?:\. | )/)?.[1]||"—";
  };
  const name=side=>linkPath(props[`${side}_team`]).split("/").pop();
  const basis=(side,category,kind)=>{
    const lines=(raw[`engine-${side}-${category}`]||"").split("\n");
    for(const line of lines){
      const evidence=splitCells(line)[2]||"";
      const match=kind==="sample"?evidence.match(/Season situational inputs as of (.+); sample ([^.]+)\./):evidence.match(/(\d{4}) season aggregate/);
      if(match)return kind==="sample"?`${match[2]} games · ${match[1]}`:match[1];
    }return "—";
  };
  const brief=["## Matchup evidence", "", `**Initial quote:** ${cell(props.sportsbook||"Book unknown")} · home spread ${props.line??"—"} · total ${props.total??"—"} · price ${props.price??"—"}${props.price_selection?" · "+cell(props.price_selection):""}.`];
  for(const side of ["away","home"]) {
    const opposing=side==="home"?"away":"home";
    brief.push("",`### ${name(side)} offense vs. ${name(opposing)} defense`,"",
      `| Season metric | ${cell(name(side))} offense | ${cell(name(opposing))} defense / allowed |`,"| --- | --- | --- |",
      `| Season | ${basis(side,"offense","season")} | ${basis(opposing,"defense","season")} |`,
      ...[["Success rate","success_rate"],["EPA / play","epa"],["Dropback EPA","pass_epa"],["Rush EPA","rush_epa"],["Explosive rate","explosiveness"]].map(([label,key])=>`| ${label} | ${metric(side,"offense",key)} | ${metric(opposing,"defense",key)} |`),
      "",`**Offense season pace:** ${metric(side,"offense","seconds_per_play")} seconds/play · neutral pass rate ${metric(side,"offense","pass_rate")} · sample ${basis(side,"offense","sample")}.`);
  }
  const source=props.engine_api_url&&props.engine_game_id?sourceLink(`${props.engine_api_url}/nfl/games/${props.engine_game_id}/`,"Engine metrics and projection inputs"):"See full source archive";
  brief.push("", `Basis: season EPA and success/explosive rates; offense pace and neutral pass rate as labeled above. ${source}. Raw contextual metrics, not opponent-adjusted betting edges.`);
  brief.push("", "### Game conditions", "", ...(raw["engine-game-context"]||"No imported conditions yet.").split("\n").filter(l=>l&&!l.startsWith("##")&&!l.includes("nfl:provenance")&&!l.startsWith("[Engine game]")).flatMap(line=>[line,""]));
  if(raw["engine-projection"])brief.push("",raw["engine-projection"].replace(/<!--[^\n]*-->\n?/g,"").replace(/## Imported projection/,"### Projection"));
  if(/Partial import|Retrospective import/.test(raw["engine-import-status"]||""))brief.push("",...raw["engine-import-status"].split("\n").filter(l=>/Partial import|Retrospective import/.test(l)));
  // Headline matching is a conservative relevance screen, never confirmed ownership or availability.
  const normalize=v=>String(v).toLowerCase().replace(/[^a-z0-9]/g,"");
  const names=[normalize(name("home")),normalize(name("away"))];
  const cutoff=Date.parse(day+"T00:00:00Z")-7*86400000;
  const all=new Map();
  for(const line of (raw["engine-reporting"]||"").split("\n"))if(line.startsWith("|")){
    const c=splitCells(line);if(c.length===5&&c[0]!=="Item"&&!/^:?-+:?$/.test(c[0]))all.set(c[4],{title:c[0],role:c[1],date:c[2],source:c[3]});
  }
  for(const side of ["home","away"])for(const line of (raw[`engine-${side}-context`]||"").split("\n"))if(line.startsWith("|")){
    const c=splitCells(line);if(!c[5]?.startsWith("availability:"))continue;
    const evidence=c[2],title=evidence.split(". Role: ")[0],date=evidence.match(/Published (\d{4}-\d{2}-\d{2})/)?.[1]||"unknown";
    const source=evidence.match(/\[[^\]]+\]\(<[^>]+>\)/)?.[0]||"Source unavailable";
    const id=c[5].split(":").slice(2).join(":");if(!all.has(id))all.set(id,{title,role:"Availability headline; verify",date,source});
  }
  const candidates=[...all.values()].filter(r=>names.every(n=>n&&normalize(r.title).includes(n))&&Date.parse(r.date)>=cutoff&&String(r.date)<=day);
  brief.push("", "### Current matchup reporting — verify", "", "Recent headlines naming both teams. Matching names does not confirm the subject team, an absence, or report accuracy. Older and uncertain items remain in the source archive.", "");
  if(candidates.length){
    candidates.sort((a,b)=>b.date.localeCompare(a.date));
    for(const [heading,pattern] of [["Reporting / official sources",/REPORTING|OFFICIAL/],["Analysis",/ANALYSIS/],["Commentary / other",null]]){
      const group=candidates.filter(r=>pattern?pattern.test(r.role):!/REPORTING|OFFICIAL|ANALYSIS/.test(r.role));
      if(group.length)brief.push(`#### ${heading}`,"",...group.map(r=>`- **${r.date}** — ${r.title}. ${r.source}`),"");
    }
  }
  else brief.push("No recent headlines confidently matched both team names. Review the source archive.");
  if(!Object.keys(raw).length)brief.splice(3,brief.length-3,"No engine evidence imported yet. Use the import command from the dashboard.");
  text=replaceBlock(text,"engine-brief",brief.join("\n"));
  // Move the generated brief before manual game context; source blocks remain where they were.
  const b=block(text,"engine-brief"),part=text.slice(b.a,b.end);
  const oldAnchor=text.indexOf("## Game context");
  if(oldAnchor<0||oldAnchor<b.end||text.slice(b.end,oldAnchor).trim()){
    text=text.slice(0,b.a)+text.slice(b.end);
    const anchor=text.indexOf("## Game context");
    text=anchor>=0?text.slice(0,anchor)+part+"\n\n"+text.slice(anchor):text+"\n"+part;
  }
  for(const [id,content] of Object.entries(raw))text=replaceBlock(text,id,folded(id.replace("engine-", "Full source: ").replaceAll("-"," "),content));
  for(const side of ["away","home"])for(const cat of ["offense","defense","context"]){
    const id=`${side}-${cat}`,original=block(text,id);
    if(!original)continue;
    const rows=original.content.split("\n").filter(l=>l.trim().startsWith("|")).map(splitCells)
      .filter(c=>c[0]!=="+"&&!/^:?-+:?$/.test(c[0])&&c.slice(0,3).some(Boolean));
    text=replaceBlock(text,id,rows.length?folded("Previous table notes — preserved",original.content):"");
    const viewId=`observations-${side}-${cat}`;
    if(!block(text,viewId)){
      const section=block(text,id);text=text.slice(0,section.end)+`\n\n<!-- nfl:${viewId}:start -->\n${observationView(side,cat)}\n<!-- nfl:${viewId}:end -->`+text.slice(section.end);
    }
  }
  const grid="<!-- nfl:observation-grid:start -->\n> [!abstract]- Edit observation properties in a grid\n> ![["+BASE+"/Views/matchup-observations.base]]\n<!-- nfl:observation-grid:end -->";
  if(!block(text,"observation-grid")) {
    const pos=text.indexOf("## Decision and next action");
    text=pos>=0?text.slice(0,pos)+grid+"\n\n"+text.slice(pos):text+"\n\n"+grid;
  }
  return text.replace("## Engine imports\n\nOnly the marked imported blocks below are refreshed. Your sections above remain editable.", "## Full source archive\n\nExpand a section for the complete imported evidence and original links. All items are retained; IDs support refresh and deduplication.");
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
// Presentation only: exact market quotes and Properties are never rounded.
function cleanDisplay(text) {
  const number = (value, places) => Number.isFinite(Number(value)) ? String(Number(Number(value).toFixed(places))) : value;
  return text.replace(/(<!-- nfl:engine-[\w-]+:start -->)([\s\S]*?)(<!-- nfl:engine-[\w-]+:end -->)/g, (_,start,body,end) => {
    if(start.includes("engine-brief:"))return start+body+end;
    const wasFolded=body.trim().startsWith("> [!abstract]-");
    const title=wasFolded?body.trim().split("\n")[0].replace("> [!abstract]- ",""):null;
    if(wasFolded)body="\n"+unfold(body.trim())+"\n";
    const stamps = [...new Set(body.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})/g)||[])];
    body = body.replace(/\n?<!-- nfl:provenance:[^\n]*-->\n?/g,"\n")
      .replace(/Fetched: \S+\. /g,"").replace(/ Imported \S+\. /g," ")
      .replace(/; fetched \S+\./g,".").replace(/ · quote fetched \S+\./g,".")
      .replace(/ Forecast issued [^;]+; source /g," Source: ")
      .replace(/(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})/g,"$1")
      .replace(/((?:EPA \/ play|Dropback EPA|Rush EPA|Explosive rate): )(-?\d+(?:\.\d+)?)/g,(_,label,n)=>label+number(n,2))
      .replace(/((?:Seconds \/ play|Yards \/ dropback|Yards \/ rush)(?: allowed)?: )(-?\d+(?:\.\d+)?)/g,(_,label,n)=>label+number(n,1))
      .replace(/(-?\d+(?:\.\d+)?) (°F|mph)/g,(_,n,unit)=>number(n,0)+" "+unit)
      .replace(/(-?\d+(?:\.\d+)?)%/g,(_,n)=>number(n,1)+"%");
    if(start.includes("engine-projection:")) body=body.split("\n").map(line=>{
      if(!line.trim().startsWith("|"))return line;
      const c=splitCells(line);
      if(c.length!==4 || c[0]==="Team" || /^:?-+:?$/.test(c[0]))return line;
      return `| ${[c[0],...c.slice(1).map(v=>v!==""?number(v,1):v)].map(cell).join(" | ")} |`;
    }).join("\n");
    const cleaned=body.trim()+(stamps.length?`\n<!-- nfl:provenance: ${stamps.join(" ")} -->`:"");
    return start+"\n"+(wasFolded?folded(title,cleaned):cleaned)+"\n"+end;
  });
}
function gameValid(game,id){
  if(!game||String(game.game_id)!==String(id)||!/^\d{4}_\d{2}_[A-Z0-9]{2,3}_[A-Z0-9]{2,3}$/.test(String(id))||!game.home_team||!game.away_team||
    !Number.isInteger(Number(game.season))||!game.home_team_id||!game.away_team_id||game.home_team_id===game.away_team_id)
    throw new Error("Engine returned an invalid or mismatched NFL game packet.");
  dateInZone(game.start_date);
}
function buildCommonImported(packet, provider, importedAt, base, zone = "America/New_York") {
  const {game, preview, situation, content, errors = []} = packet;
  const source = `${base}/nfl/games/${game.game_id}/`;
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
    if(provider) context.push("", `**Latest market:** ${cell(provider.provider)} · home spread ${provider.spread ?? "unavailable"} · total ${provider.over_under ?? "unavailable"} · quote fetched ${provider.fetched_at||"time unavailable"}. Opening home spread ${provider.spread_open ?? "unavailable"}; opening total ${provider.over_under_open ?? "unavailable"}. Prices are source-specific; check the spread/total price fields. The schedule feed does not identify a sportsbook.`);
    else context.push("", "**Market:** no selected provider quote available; no consensus quote substituted.");
    imported["engine-game-context"]=context.join("\n");
  }
  for(const side of ["home","away"]) {
    const metrics=preview?.[`${side}_quality`]?.metrics?.advanced || game.advanced_metrics?.[game[`${side}_team`]];
    for(const category of ["offense","defense"]) {
      const items=[];
      const definitions=[["success_rate","Success rate"],["epa","EPA / play"],["explosiveness","Explosive rate"],["pass_epa","Dropback EPA"],["rush_epa","Rush EPA"]];
      if(metrics) for(const [key,label] of definitions) {
        const value=metrics[`${category}_${key}`];
        if(value!=null) items.push(fact(`metric:${side}:${category}:${key}`,`${label}: ${key==="success_rate"||key==="havoc"||key==="explosiveness"?(100*Number(value)).toFixed(1)+"%":value}. ${metrics.season??game.season} season aggregate; not an opponent-adjusted betting edge. Imported ${importedAt}. ${sourceLink(source,"engine metrics")}`));
      }
      if(preview?.projection) {
        const values=preview.projection[`${side}_snapshot`];
        for(const [key,label] of [["seconds_per_play","Seconds / play"],["pass_rate","Pass rate"],["yards_per_dropback","Yards / dropback"],["yards_per_rush","Yards / rush"]]) {
          const field=category==="offense"?key:`${key}_allowed`;
          if(values?.[field]!=null) items.push(fact(`pace:${side}:${category}:${key}`,`${label}${category==="defense"?" allowed":""}: ${key==="pass_rate"?(100*Number(values[field])).toFixed(1)+"%":Number(values[field]).toFixed(2)}. Season situational inputs as of ${preview.projection.as_of_date||"unspecified"}; sample ${values.games??"unspecified"}. ${sourceLink(source,"engine projection inputs")}`));
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
  const props={type:"nfl-matchup",season:game.season,week:String(game.week),game_date:day,
    home_team:link(teamPaths.home),away_team:link(teamPaths.away),line:null,total:null,price:null,
    price_market:"",price_selection:"",home_spread_price:null,away_spread_price:null,over_price:null,under_price:null,
    home_moneyline:null,away_moneyline:null,sportsbook:"",market_timestamp:"",neutral_site:!!game.neutral_site,
    venue:game.venue||"",kickoff:game.start_date,game_context:"",status:"research",next_action:"",review_date:null,
    home_score:null,away_score:null};
  let text=`---\n${Object.entries(props).map(([k,v])=>`${k}: ${scalar(v)}`).join("\n")}\ntags:\n  - football/nfl/matchup\n---\n\n# ${game.away_team} at ${game.home_team}\n\n`;
  text+=`**Away:** ${link(teamPaths.away)} · **Home:** ${link(teamPaths.home)}\n\n[[${BASE}/NFL Dashboard|NFL Dashboard]]\n\n`;
  text+="> line = HOME spread. price = American odds for price_market + price_selection. Initial quoted fields are preserved on refresh; engine_* fields and imported market context show the latest quote.\n\n";
  text+="## Game context\n\n<!-- nfl:game-context:start -->\n- Weather / surface:\n- Rest / travel / scheduling:\n- Stakes / rivalry:\n- Game script:\n<!-- nfl:game-context:end -->\n\n";
  for(const side of ["away","home"]) {
    text+=`## ${side==="home"?"Home":"Away"} team\n\n${link(teamPaths[side])}\n\n`;
    for(const cat of ["Offense","Defense","Context"])
      text+=`### ${cat}\n\n<!-- nfl:${side}-${cat.toLowerCase()}:start -->\n| + | - | Evidence / condition | Carry forward | Review / expires |\n| --- | --- | --- | --- | --- |\n| | | | game-only | |\n<!-- nfl:${side}-${cat.toLowerCase()}:end -->\n\n`;
  }
  return text+"## Decision and next action\n\n- Thesis:\n- Conflicting evidence:\n- Price / information needed:\n- Decision:\n- Pregame conclusion and timestamp:\n\n## Postgame review\n\n- What happened:\n- What held up / changed:\n- Carry-forward rows updated:\n\n## Sources\n\n- \n\n## Engine imports\n\nOnly the marked imported blocks below are refreshed. Your sections above remain editable.\n";
}
function refresh(text,packet,provider,teamPaths,importedAt,base,parseYaml,zone) {
  const game=packet.game;
  const fm=frontmatter(text,parseYaml).data;
  if(fm.type!=="nfl-matchup")throw new Error("Target is not a NFL matchup note.");
  if(fm.engine_game_id && String(fm.engine_game_id)!==String(game.game_id))throw new Error("Target belongs to another engine game.");
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
      sportsbook:provider.provider,market_timestamp:provider.fetched_at,home_spread_price:provider.home_spread_price,away_spread_price:provider.away_spread_price,over_price:provider.over_price,under_price:provider.under_price} : {};
  text=patchProperties(text,updates,parseYaml,{...initialMarket,venue:game.venue,kickoff:game.start_date,
    home_score:game.completed?game.home_points:null,away_score:game.completed?game.away_points:null});
  for(const [id,value] of Object.entries(buildImported(packet,provider,importedAt,base,zone))) {
    const merged=Array.isArray(value)?mergeRows(block(text,id)?.content,value):
      id==="engine-reporting"?mergeReporting(block(text,id)?.content,value):value;
    text=replaceBlock(text,id,merged);
  }
  return organize(cleanDisplay(text),parseYaml);
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
    throw new Error("Run Engine Import or Freeze from the NFL Dashboard, not from a matchup being edited.");
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
    if(props.type==="nfl-team")pages.push({f,props});
  }
  let matches=pages.filter(x=>String(x.props.engine_team_id)===String(id));
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
  body=patchProperties(body,{engine_team_id:id},parseYaml,{conference:game[`${side}_conference`],division:game[`${side}_division`]});
  await ensureFolder(vault,`${BASE}/Teams`); await vault.create(target,body); return target;
}
async function run(tp) {
  prohibitActiveMatchup(tp);
  const config=await configuration(tp), base=config.api_url, parseYaml=tp.obsidian.parseYaml;
  let input=await tp.system.prompt("Paste engine game URL or ID. Leave blank to select an upcoming game.","",true);
  if(input===null)throw new Error("Cancelled.");
  let id;
  if(input.trim())id=parseGameInput(input,base);
  else{
    const year=new Date().getFullYear()-(new Date().getMonth()<2?1:0);
    const season=await tp.system.prompt("NFL season (year the season began)",String(year),true);
    if(season===null)throw new Error("Cancelled.");if(!/^\d{4}$/.test(season))throw new Error("Season must have four digits.");
    const slate=await request(tp,`${base}/api/v1/nfl?season=${season}`);
    const games=(slate.matchups||[]).filter(g=>!g.completed);
    if(!games.length)throw new Error("No upcoming games on this slate. Paste an NFL game URL or ID instead.");
    const selected=await tp.system.suggester(games.map(g=>`${g.date} · week ${g.week} · ${(g.sides||[]).find(s=>s.role==="away")?.name||"Away"} at ${(g.sides||[]).find(s=>s.role==="home")?.name||"Home"}`),games,true,"Choose an NFL game");
    if(!selected)throw new Error("Cancelled.");id=parseGameInput(selected.game_id,base);
  }
  const packet=normalizeNFL(await request(tp,`${base}/api/v1/nfl/games/${id}`),id);
  const game=packet.game;
  const providers=(packet.situation?.lines?.providers||[]).filter(p=>p.provider);
  let provider=null;
  if(providers.length) {
    const preferred=providers.find(p=>nameKey(p.provider)===nameKey(config.preferred_sportsbook));
    const ordered=preferred?[preferred,...providers.filter(p=>p!==preferred)]:providers;
    provider=await tp.system.suggester(ordered.map(p=>`${p.provider}: home ${p.spread??"—"}, total ${p.over_under??"—"}`),ordered,true,"Choose supplied market source (book may be unspecified)");
    if(!provider)throw new Error("Cancelled.");
  }
  // Required requests and sportsbook selection finish before any note writes.
  const teamPaths={away:await teamPath(tp,game,"away",parseYaml),home:await teamPath(tp,game,"home",parseYaml)};
  const vault=tp.app.vault, candidates=[];
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))) {
    const text=await vault.read(file);
    if(!text.startsWith("---"))continue;
    const props=frontmatter(text,parseYaml).data;
    if(props.type!=="nfl-matchup")continue;
    const exact=String(props.engine_game_id)===id;
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
    if(raw.startsWith("---") && frontmatter(raw,tp.obsidian.parseYaml).data.type==="nfl-matchup")files.push(f);
  }
  if(!files.length)throw new Error("No matchup notes to snapshot.");
  const file=await tp.system.suggester(files.map(f=>f.basename),files,true,"Select matchup to freeze before kickoff");
  if(!file)throw new Error("Cancelled.");
  const text=await vault.read(file), props=frontmatter(text,tp.obsidian.parseYaml).data;
  const kickoff=Date.parse(props.engine_kickoff||props.kickoff), now=new Date();
  if(props.type!=="nfl-matchup" || !Number.isFinite(kickoff))throw new Error("Import this matchup first so its kickoff is known.");
  if(props.engine_completed || kickoff<=now.getTime())throw new Error("Kickoff has passed; a pregame snapshot cannot be created now.");
  const stamp=now.toISOString(), target=`${BASE}/Snapshots/${file.basename} - ${stamp.replace(/[:.]/g,"-")}.md`;
  let frozen=text.replace(/<!-- nfl:observation-grid:start -->[\s\S]*?<!-- nfl:observation-grid:end -->/g,"Frozen observations are included below.").replace(/```dataviewjs\nawait dv\.view\("[^"\n]*nfl-observation-view"[^\n]*\);\n```/g,"See the frozen observations below.");
  frozen+="\n\n## Frozen observations\n\n";
  for(const observation of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Observations/`))) {
    const body=await vault.read(observation);if(!body.startsWith("---"))continue;
    const data=frontmatter(body,tp.obsidian.parseYaml).data;
    if(data.type!=="nfl-observation" || linkPath(data.matchup)!==linkPath(file.path))continue;
    const m=frontmatter(body,tp.obsidian.parseYaml).match;
    frozen+=`### ${cell(data.observation||observation.basename)}\n\n\`\`\`yaml\n${m[1]}\n\`\`\`\n\n${body.slice(m[0].length).replace(/```dataviewjs[\s\S]*?```/g,"")}\n\n`;
  }
  const snapshot=patchProperties(frozen,{type:"nfl-pregame-snapshot",snapshot_at:stamp,source_matchup:link(file.path)},tp.obsidian.parseYaml);
  await ensureFolder(vault,`${BASE}/Snapshots`);
  if(vault.getAbstractFileByPath(target))throw new Error("Snapshot already exists. Retry in a moment.");
  await vault.create(target,snapshot);
  new tp.obsidian.Notice("Saved pregame snapshot, including your analysis and imported evidence.",8000);
  return target;
}
// Adapt the NFL packet; identity, kickoff, market signs, and metric bases are explicit.
function kickoffEastern(date,time){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(String(date))||!/^\d{2}:\d{2}$/.test(String(time)))throw new Error("NFL game has no valid kickoff time.");
  const [hour,minute]=time.split(":").map(Number);if(hour>23||minute>59)throw new Error("Invalid NFL kickoff time.");
  const target=Date.parse(`${date}T${time}:00Z`);if(!Number.isFinite(target)||new Date(target).toISOString().slice(0,10)!==date)throw new Error("Invalid NFL game date.");
  let instant=target;
  for(let i=0;i<3;i++){
    const parts=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).formatToParts(new Date(instant));
    const get=k=>parts.find(p=>p.type===k).value;
    const local=Date.parse(`${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}:${get("second")}Z`);
    instant+=target-local;
  }
  return new Date(instant).toISOString();
}
function normalizeNFL(raw,id){
  const g=raw?.game;if(!g||String(g.game_id)!==String(id))throw new Error("NFL packet belongs to a different game.");
  const game={...g,start_date:kickoffEastern(g.game_date,g.game_time),venue:g.stadium,home_points:g.home_score,away_points:g.away_score,neutral_site:!!g.neutral_site,completed:!!g.completed};
  for(const side of ["home","away"]){
    const identity=raw[`${side}_identity`];if(!identity?.name||identity.abbreviation!==g[`${side}_team`])throw new Error(`Missing or mismatched NFL ${side} identity.`);
    game[`${side}_team_id`]=identity.abbreviation;game[`${side}_team`]=identity.name;game[`${side}_conference`]=identity.conference;game[`${side}_division`]=identity.division;
  }
  gameValid(game,id);
  const errors=[];const present=k=>Object.hasOwn(raw,k)&&raw[k]!=null;
  const preview={};
  if(present("profiles"))for(const side of ["home","away"]){
    const row=raw.profiles[game[`${side}_team_id`]]||{};
    preview[`${side}_quality`]={metrics:{advanced:{season:raw.baseline_season??g.season,
      offense_success_rate:row.success_rate,defense_success_rate:row.defensive_success_allowed,
      offense_epa:row.epa_per_play,defense_epa:row.defensive_epa_allowed,
      offense_explosiveness:row.explosive_rate,defense_explosiveness:row.defensive_explosive_allowed,
      offense_pass_epa:row.pass_epa_per_play,defense_pass_epa:row.defensive_pass_epa_allowed,
      offense_rush_epa:row.rush_epa_per_play,defense_rush_epa:row.defensive_rush_epa_allowed}}};
  }else errors.push("season profiles unavailable");
  const projection=raw.football_lab?.football_lab,shape=raw.game_shape||{};
  if(projection||present("game_shape")){
    preview.projection={as_of_date:`${raw.baseline_season??g.season} season; before week ${g.week}${raw.baseline_season&&raw.baseline_season!==g.season?"; prior-season fallback":""}`,insufficient_data:!!raw.football_lab?.thin_sample,
      points_model:{model_version:projection?`Football Lab ${projection.margin_model||"margin"} / ${projection.total_model||"total"}`:"game-shape heuristic (includes market midpoint)"}};
    for(const side of ["home","away"]){
      const component=raw.football_lab?.components?.[side]||{},snapshot=shape.situational?.[game[`${side}_team_id`]]||{};
      preview.projection[side]={expected_points:projection?.[`${side}_points`]??shape[`${side}_points`],drives:component.drives??shape.expected_possessions_per_team,
        plays:component.drives!=null&&component.plays_per_drive!=null?component.drives*component.plays_per_drive:shape.expected_plays_per_team};
      preview.projection[`${side}_snapshot`]={games:snapshot.games,seconds_per_play:snapshot.seconds_per_play,pass_rate:snapshot.neutral_pass_rate};
    }
  }else errors.push("projection unavailable");
  const m=raw.market;let providers=[];
  if(m){providers=[{provider:m.provider||"nflverse schedule (book unspecified)",spread:m.line==null?null:-Number(m.line),over_under:m.total,
    home_moneyline:m.home_moneyline,away_moneyline:m.away_moneyline,home_spread_price:m.home_spread_odds,away_spread_price:m.away_spread_odds,
    over_price:m.over_odds,under_price:m.under_odds,fetched_at:g.updated_at}];}else errors.push("market unavailable");
  // The NFL API's situation list contains supplied rest/venue conditions, not a CFB weather endpoint.
  let situation;
  if(present("situation")||m){situation={lines:{providers},schedule_spots:[],availability:[]};}
  if(!present("situation"))errors.push("schedule conditions unavailable");
  const content=present("content")?{reporting:Array.isArray(raw.content)?raw.content.map(item=>({...item,publisher_name:item.source_name,
    source_role:item.content_type||"reporting",relevance_label:(item.games||[]).some(x=>x.game_id===id)?"Game-linked":"Team context"})):[]}:null;
  if(!present("content"))errors.push("reporting unavailable");
  if(!present("availability"))errors.push("availability unavailable");
  return {game,preview:Object.keys(preview).length?preview:null,situation,content,errors,raw};
}
function displayData(value,key=""){
  if(Array.isArray(value))return value.map(v=>displayData(v));
  if(value&&typeof value==="object")return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,displayData(v,k)]));
  if(typeof value==="string")return value.replace(/(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})/g,"$1");
  if(typeof value==="number"&&!Number.isInteger(value)&&!/(odds|price|line|moneyline|spread|total)$/.test(key))return Number(value.toFixed(2));return value;
}
function buildImported(packet,provider,importedAt,base,zone="America/New_York"){
  const imported=buildCommonImported(packet,provider,importedAt,base,zone),raw=packet.raw;
  if(!raw)return imported;
  const game=packet.game,source=`${base}/nfl/games/${game.game_id}/`,review=dateInZone(game.start_date,zone);
  if(packet.situation){
    let context=imported["engine-game-context"].replace("**Weather:** no forecast available.","**Weather:** dedicated forecast not supplied by this NFL packet; verify stadium, roof, and conditions before acting.");
    for(const item of Array.isArray(raw.situation)?raw.situation:[])context+=`\n\n**${cell(item.label)}:** ${cell(item.value)}${item.detail?" · "+cell(item.detail):""}.`;
    if(game.division_game)context+="\n\n**Division matchup.**";
    if(/Tottenham|Wembley|Munich|Frankfurt|Berlin|Madrid|Sao Paulo|São Paulo|Melbourne|Mexico|Dublin/i.test(game.venue||""))context+="\n\n**International venue:** designated home/away may differ from ordinary home-field conditions. Verify travel and site assumptions; source neutral-site flag is preserved.";
    const nflWeather=raw.football_lab?.football_lab?.weather;
    if(nflWeather)context+=`\n\n**Supplied projection weather:** ${cell(JSON.stringify(displayData(nflWeather)))}.`;
    imported["engine-game-context"]=context;
  }
  if(raw.availability)for(const side of ["away","home"]){
    const rows=imported[`engine-${side}-context`]||[];
    for(const item of raw.availability[game[`${side}_team_id`]]?.rows||[]){
      const id=item.injury_id??item.gsis_id??item.espn_id??item.player_name;
      rows.push(fact(`injury:${side}:${id}`,`${item.player_name} (${item.position||"position unspecified"}): ${item.designation_label||item.status||"status unspecified"}. ${item.detail_line||item.detail||""}${item.practice_status?" Practice: "+item.practice_status+".":""} Reported ${item.report_date||"date unavailable"}. ${item.short_comment||""} ${item.long_comment||""} ${sourceLink(item.source_url||source,"availability source")}. Reported status, not a confirmed game inactive.`,review,"watch"));
    }
    imported[`engine-${side}-context`]=rows;
  }
  if(Array.isArray(raw.content)){
    imported["engine-report-details"]=raw.content.filter(item=>item.content_id||item.canonical_url).map(item=>fact(`report:${item.content_id??item.canonical_url}`,`${item.title||"Untitled"}. ${sourceLink(item.canonical_url,item.source_name||"source")} · ${String(item.published_at||"Date unavailable").slice(0,10)}. ${item.body_text||"No excerpt supplied."}`));
  }
  const details=Object.fromEntries(["baseline_season","pass_profile_season","pff_season","football_lab","football_lab_status","injury_impact","personnel","usage","trenches","defense_matchups","position_group_matchups","run_matchups","production","line_movement"].filter(k=>Object.hasOwn(raw,k)).map(k=>[k,raw[k]]));
  if(Object.keys(details).length)imported["engine-nfl-details"]=`## Supplied model and personnel details\n\n${sourceLink(source,"Engine detail")} · Rounded displays; original quote fields stay exact.\n\n\`\`\`json\n${JSON.stringify(displayData(details),null,2)}\n\`\`\``;
  return imported;
}

return {run,freeze,refresh,buildImported,emptyNote,mergeRows,patchProperties,parseGameInput,dateInZone,baseUrl,gameValid,cleanDisplay,block,replaceBlock,splitCells,cell,organize,unfold,workflow,normalizeNFL,kickoffEastern};
