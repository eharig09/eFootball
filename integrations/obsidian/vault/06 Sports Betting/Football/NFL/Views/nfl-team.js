// Dataview custom view: pulls only the current team's sections, preserving source provenance.
const BASE = "06 Sports Betting/Football/NFL";
const current = dv.current();
const mode = input?.mode ?? "games";
const norm = value => String(value?.path ?? value ?? "").replace(/^\[\[/," ").trim()
  .replace(/\]\]$/," ").trim().split("|")[0].replace(/\.md$/,"");
const self = norm(current.file.path);
const display = value => value === null || value === undefined || value === "" ? "—" : value;
const dateText = value => value?.toFormat ? value.toFormat("yyyy-MM-dd") : String(value ?? "");
function section(text, id) {
  const start = `<!-- nfl:${id}:start -->`, end = `<!-- nfl:${id}:end -->`;
  const pos = text.indexOf(start);
  if (pos < 0) return "";
  const stop = text.indexOf(end,pos+start.length);
  if(stop<0)return "";
  const content=text.slice(pos+start.length,stop).trim();
  return content.startsWith("> [!abstract]-")?content.split(/\r?\n/).slice(1).map(line=>line.replace(/^> ?/,"")).join("\n").trim():content;
}
function cells(line) {
  // Escaped pipes and wiki-link aliases are kept inside their cells.
  const out=[]; let cell="", wiki=0;
  line=line.trim().replace(/^\|/,"").replace(/\|$/,"");
  for(let i=0;i<line.length;i++) {
    if(line[i]==="\\" && line[i+1]==="|") {cell+="|";i++;continue;}
    if(line.slice(i,i+2)==="[[") {wiki++;cell+="[[";i++;continue;}
    if(line.slice(i,i+2)==="]]") {wiki=Math.max(0,wiki-1);cell+="]]";i++;continue;}
    if(line[i]==="|" && !wiki) {out.push(cell.trim());cell="";} else cell+=line[i];
  }
  out.push(cell.trim()); return out;
}
const migrated=Array.from(dv.pages(`"${BASE}/Observations"`)).filter(p=>p.type==="nfl-observation").map(p=>String(p.legacy_key||""));
function rows(text, prefix="") {
  return text.split(/\r?\n/).map((line,index)=>({cells:cells(line),index,line})).filter(item=>item.line.trim().startsWith("|")).filter(item=>!migrated.includes(prefix+"::"+(item.cells[5]||item.index))).map(item=>item.cells).filter(c=>c.length>=5 && c[0]!=="+" && !/^:?-+:?$/.test(c[0]) && (c[0]||c[1]||c[2]))
    .map(c=>({plus:c[0],minus:c[1],evidence:c[2],carry:c[3].toLowerCase().trim(),review:c[4]}));
}
function reviewLabel(row) {
  const match=row.review.match(/^\d{4}-\d{2}-\d{2}/);
  const today=dv.date("today").toFormat("yyyy-MM-dd");
  return match && match[0]<=today ? `REVIEW DUE — ${row.review}` : display(row.review);
}
const pages=Array.from(dv.pages(`"${BASE}/Matchups"`))
  .filter(p=>p.type==="nfl-matchup" && (norm(p.home_team)===self || norm(p.away_team)===self)
    && (!current.season || String(p.season)===String(current.season)))
  .sort((a,b)=>dateText(b.game_date).localeCompare(dateText(a.game_date)) || a.file.path.localeCompare(b.file.path));
if(mode!=="games")await dv.view(`${BASE}/Views/nfl-observation-view`,{active:mode==="active",category:mode==="active"?undefined:mode});
if(!pages.length) { dv.paragraph("No matchups for this team and season yet. Create a matchup using NFL Matchup Template; clear season to show all seasons."); return; }
const games=[];
for(const p of pages) {
  const side=norm(p.home_team)===self ? "home" : "away";
  const opponent=side==="home" ? p.away_team : p.home_team;
  const raw=await dv.io.load(p.file.path);
  if(raw===undefined) {dv.paragraph(`Could not read ${p.file.path}. Wait for sync and reopen.`);continue;}
  const line=p.line===null || p.line===undefined || p.line==="" ? null : Number(p.line)*(side==="home"?1:-1);
  const role=p.neutral_site ? `Neutral (${side})` : side==="home"?"Home":"Away";
  const categories=Object.fromEntries(["offense","defense","context"].map(c=>[c,[...rows(section(raw,`${side}-${c}`),`${p.file.path}::${side}-${c}`),...rows(section(raw,`engine-${side}-${c}`),`${p.file.path}::engine-${side}-${c}`).filter(r=>r.plus||r.minus)]]));
  games.push({p,side,opponent,line,role,categories,fullContext:[section(raw,"game-context"),section(raw,"engine-import-status"),section(raw,"engine-game-context"),section(raw,"engine-reporting")].filter(Boolean).join("\n\n")});
}
const gameLink=g=>dv.fileLink(g.p.file.path,false,`${dateText(g.p.game_date)} · W${g.p.week||"?"}`);
function provenance(g) {
  const s=g.line===null?"—":`${g.line>0?"+":""}${g.line}`;
  return `${g.role} · Team spread ${s} · Total ${display(g.p.total)}`;
}
function rowTable(records, withCategory=false) {
  const headers=["Game","Opponent","Game context",...(withCategory?["Section"]:[]),"+","-","Evidence / condition","Carry forward","Review / expires"];
  dv.table(headers,records.map(({g,c,r})=>[
    gameLink(g),g.opponent,`${provenance(g)}${g.p.game_context?" · "+g.p.game_context:""}`,
    ...(withCategory?[c]:[]),display(r.plus),display(r.minus),display(r.evidence),display(r.carry),reviewLabel(r)
  ]));
}
if(mode==="games") {
  dv.table(["Game","Opponent","Venue / role","Team spread","Total","Price / selection","Status"],games.map(g=>[
    gameLink(g),g.opponent,g.role,display(g.line),display(g.p.total),
    g.p.price===null||g.p.price===undefined||g.p.price===""?"—":`${g.p.price} · ${g.p.price_market||"market unspecified"} · ${g.p.price_selection||"selection unspecified"}`,
    display(g.p.status)
  ]));
  for(const g of games) {
    dv.header(3,`${dateText(g.p.game_date)} · ${g.role}`);
    dv.paragraph(`${gameLink(g)} · Opponent: ${g.opponent}\n\n${provenance(g)}`);
    if(g.p.game_context) dv.paragraph(g.p.game_context);
    dv.paragraph(section(await dv.io.load(g.p.file.path),"game-context") || "No manual game context entered.");
    dv.paragraph(dv.fileLink(g.p.file.path,false,"Open matchup comparisons and full source archive"));
    dv.paragraph(dv.sectionLink(g.p.file.path,g.side==="home"?"Home team":"Away team",false,"Open this team's source notes"));
  }
} else {
  const records=[];
  for(const g of games) for(const [c,notes] of Object.entries(g.categories)) {
    for(const r of notes) if(mode==="active"?["active","watch"].includes(r.carry):c===mode) records.push({g,c,r});
  }
  if(records.length) rowTable(records,mode==="active");

}
