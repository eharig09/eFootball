// Atomic observation notes, captured with a native form and managed through Bases.
const BASE="06 Sports Betting/Football/CFB";
const norm=value=>String(value?.path??value??"").replace(/^\[\[|\]\]$/g,"").split("|")[0].replace(/\.md$/,"");
const link=path=>`[[${norm(path)}]]`;
const q=value=>JSON.stringify(value);
function properties(text,parseYaml){
  const m=text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if(!m)throw new Error("Note has no Properties.");return {data:parseYaml(m[1]),match:m};
}
function serialize(game,file,values,id,legacyKey="") {
  if(!String(values.observation||"").trim())throw new Error("Enter an observation.");
  if(!["home","away"].includes(values.side)||!["offense","defense","context"].includes(values.category))throw new Error("Invalid team or section.");
  if(!["positive","negative","neutral","mixed"].includes(values.direction))throw new Error("Invalid direction.");
  const opposite=values.side==="home"?"away":"home";
  const props={type:"cfb-observation",observation_id:id,observation:values.observation.trim(),
    team:link(game[`${values.side}_team`]),opponent:link(game[`${opposite}_team`]),matchup:link(file.path),
    season:game.season,week:game.week,game_date:game.game_date instanceof Date?game.game_date.toISOString().slice(0,10):String(game.game_date).slice(0,10),side:values.side,
    category:values.category,direction:values.direction,carry:values.carry||"game-only",review:values.review||"",
    context:game.game_context||"",venue:game.venue||"",neutral_site:!!game.neutral_site,
    home_line:game.line??null,team_line:game.line==null||game.line===""?null:Number(game.line)*(values.side==="home"?1:-1),
    total:game.total??null,price:game.price??null,price_market:game.price_market||"",price_selection:game.price_selection||"",sportsbook:game.sportsbook||"",legacy_key:legacyKey};
  return `---\n${Object.entries(props).map(([k,v])=>`${k}: ${q(v)}`).join("\n")}\n---\n\n# Observation\n\n\`\`\`dataviewjs\nawait dv.view("${BASE}/Views/cfb-observation-view", {editOnly: true});\n\`\`\`\n\n## Evidence\n\n${values.evidence||""}\n\n## Follow-up\n\n${values.followup||""}\n`;
}
function content(text,heading) {
  const start=text.indexOf(`## ${heading}\n`);if(start<0)return "";
  const rest=text.slice(start+heading.length+4);return (heading==="Evidence"?rest.split(/\n## Follow-up\n/)[0]:rest.split(/\n## /)[0]).trim();
}
async function folder(vault,path){let current="";for(const part of path.split("/")){current=current?`${current}/${part}`:part;if(!vault.getAbstractFileByPath(current))await vault.createFolder(current);}}
function modal(app,obsidian,games,initial,editing) {
  const {Modal,Setting,Notice}=obsidian;
  return new Promise(resolve=>{
    class Capture extends Modal {
      onOpen(){
        this.modalEl.style.width="min(820px, 94vw)";
        const el=this.contentEl,values={...initial};
        el.createEl("h2",{text:editing?"Edit observation":"Add observation"});
        el.createEl("p",{text:"Save an insight linked to its team, opponent, and originating matchup. Full evidence stays in the note."});
        let sideControl;
        const select=(name,key,options,disabled=false)=>new Setting(el).setName(name).addDropdown(c=>{
          for(const [value,label] of Object.entries(options))c.addOption(value,label);
          if(values[key]&&!Object.hasOwn(options,values[key]))c.addOption(values[key],values[key]);
          if(key==="side")sideControl=c;
          c.setValue(values[key]).setDisabled(disabled).onChange(v=>{
            values[key]=v;
            if(key==="matchup"&&initial.focusTeam){
              const game=games.find(g=>g.file.path===v);
              values.side=norm(game.data.home_team)===norm(initial.focusTeam)?"home":"away";
              sideControl?.setValue(values.side);
            }
          });
        });
        select("Originating matchup","matchup",Object.fromEntries(games.map(g=>[g.file.path,g.file.basename])),editing);
        select("Team","side",{away:"Away team",home:"Home team"},!!initial.focusTeam);
        select("Section","category",{offense:"Offense",defense:"Defense",context:"Context"});
        select("Direction","direction",{positive:"+ Favorable",negative:"− Unfavorable",neutral:"Neutral / question",mixed:"Mixed"});
        for(const [key,label,rows] of [["observation","Observation",3],["evidence","Evidence / sources / conditions",6],["followup","Follow-up",3]]) {
          const setting=new Setting(el).setName(label);
          setting.settingEl.style.flexDirection="column";setting.settingEl.style.alignItems="stretch";setting.controlEl.style.width="100%";
          setting.addTextArea(c=>{c.setValue(values[key]||"").onChange(v=>values[key]=v);c.inputEl.rows=rows;c.inputEl.style.width="100%";});
        }
        select("Carry forward","carry",{"game-only":"This game only",watch:"Watch",active:"Active",retired:"Retired",superseded:"Superseded"});
        new Setting(el).setName("Review date or condition").setDesc("Start with YYYY-MM-DD for a due flag, or write a condition.").addText(c=>c.setValue(values.review||"").onChange(v=>values.review=v));
        new Setting(el).addButton(c=>c.setButtonText("Cancel").onClick(()=>this.close())).addButton(c=>c.setButtonText("Save observation").setCta().onClick(()=>{
          if(!String(values.observation||"").trim()){new Notice("Enter an observation.");return;}resolve(values);this.close();
        }));
      }
      onClose(){this.contentEl.empty();resolve(null);}
    }
    new Capture(app).open();
  });
}
async function open(app,obsidian,options={}) {
  const vault=app.vault,games=[];
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))) {
    const raw=await vault.read(file);if(!raw.startsWith("---"))continue;
    const data=properties(raw,obsidian.parseYaml).data;if(data.type==="cfb-matchup"&&(!options.teamPath||[data.home_team,data.away_team].some(t=>norm(t)===norm(options.teamPath))))games.push({file,data});
  }
  games.sort((a,b)=>b.file.basename.localeCompare(a.file.basename));
  if(!games.length){new obsidian.Notice("Create or import a matchup first.");return;}
  let existing=null,original="",data=null;
  if(options.observationPath){
    existing=vault.getAbstractFileByPath(options.observationPath);if(!existing)throw new Error("Observation not found.");
    original=await vault.read(existing);data=properties(original,obsidian.parseYaml).data;
    if(data.type!=="cfb-observation")throw new Error("Select an observation note.");
  }
  const matchPath=data?norm(data.matchup)+".md":options.matchupPath;
  const selected=games.find(g=>g.file.path===matchPath)||(!data?games[0]:null);
  if(!selected)throw new Error("Originating matchup is missing. Restore it before editing this observation.");
  const values=await modal(app,obsidian,games,{matchup:selected.file.path,focusTeam:options.teamPath,side:data?.side||options.side||(options.teamPath&&norm(selected.data.home_team)===norm(options.teamPath)?"home":"away"),
    category:data?.category||options.category||"offense",direction:data?.direction||"neutral",observation:data?.observation||"",
    evidence:existing?content(original,"Evidence"):"",followup:existing?content(original,"Follow-up"):"",
    carry:data?.carry||"game-only",review:data?.review||""},!!existing);
  if(!values)return;
  const chosen=games.find(g=>g.file.path===values.matchup);if(!chosen)throw new Error("Choose a valid matchup.");
  const latest=properties(await vault.read(chosen.file),obsidian.parseYaml).data;
  if(latest.type!=="cfb-matchup")throw new Error("Originating note is no longer a matchup.");
  if(existing){
    // Keep unknown Properties and prose. Only form-owned fields and Evidence/Follow-up change.
    const helper=vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);
    const engine=new Function(await vault.read(helper))();
    const generated=serialize(latest,chosen.file,values,data.observation_id,data.legacy_key);
    const updates=properties(generated,obsidian.parseYaml).data;
    let result=engine.patchProperties(original,updates,obsidian.parseYaml);
    for(const heading of ["Evidence","Follow-up"]){
      const start=result.indexOf(`## ${heading}\n`);
      const next=start<0?-1:result.indexOf(heading==="Evidence"?"\n## Follow-up\n":"\n## ",start+4);
      const value=heading==="Evidence"?values.evidence:values.followup;
      if(start<0)result+=`\n\n## ${heading}\n\n${value||""}\n`;
      else result=result.slice(0,start)+`## ${heading}\n\n${value||""}\n`+(next<0?"":result.slice(next));
    }
    await vault.process(existing,current=>{if(current!==original)throw new Error("Observation changed while editing. Reopen the form.");return result;});
    new obsidian.Notice("Observation updated.");return existing.path;
  }
  await folder(vault,`${BASE}/Observations`);
  const id=`${Date.now().toString(36)}-${Math.random().toString(36).slice(2,10)}`;
  const title=String(values.observation).replace(/[\\/:*?"<>|#\[\]\r\n]/g,"-").slice(0,60).replace(/[. ]+$/,"")||"Observation";
  const path=`${BASE}/Observations/${id} - ${title}.md`;
  if(vault.getAbstractFileByPath(path))throw new Error("Filename collision; retry saving.");
  await vault.create(path,serialize(latest,chosen.file,values,id));
  new obsidian.Notice("Observation saved. Matchup and team views will refresh.");return path;
}
async function migrate(app,obsidian,path) {
  const vault=app.vault,file=vault.getAbstractFileByPath(path);if(!file)return [];
  const text=await vault.read(file),game=properties(text,obsidian.parseYaml).data;
  if(game.type!=="cfb-matchup")return [];
  const helper=vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);
  const engine=new Function(await vault.read(helper))();
  const known=new Set();
  for(const f of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Observations/`))){
    const body=await vault.read(f);if(body.startsWith("---"))known.add(properties(body,obsidian.parseYaml).data.legacy_key);
  }
  const saved=[];
  for(const side of ["away","home"])for(const category of ["offense","defense","context"])for(const imported of [false,true]){
    const marker=`${imported?"engine-":""}${side}-${category}`,section=engine.block(text,marker);if(!section)continue;
    const lines=section.content.split(/\r?\n/);
    for(let index=0;index<lines.length;index++){
      if(!lines[index].trim().startsWith("|"))continue;
      const c=engine.splitCells(lines[index]);
      if(c[0]==="+"||/^:?-+:?$/.test(c[0])||!c.slice(0,3).some(Boolean)||(imported&&!c[0]&&!c[1]))continue;
      const key=`${path}::${marker}::${imported?c[5]:index}`;if(known.has(key))continue;
      let hash=2166136261;for(const char of key){hash^=char.charCodeAt(0);hash=Math.imul(hash,16777619);}const id=`legacy-${(hash>>>0).toString(16)}`;
      const target=`${BASE}/Observations/${id}.md`;
      if(vault.getAbstractFileByPath(target))throw new Error("Legacy observation filename collision. Existing file preserved.");
      await folder(vault,`${BASE}/Observations`);
      await vault.create(target,serialize(game,file,{side,category,direction:c[0]&&c[1]?"mixed":c[0]?"positive":c[1]?"negative":"neutral",
        observation:c[0]&&c[1]?`+ ${c[0]} / − ${c[1]}`:c[0]||c[1]||c[2],evidence:c[2].replace(/<br>/g,"\n"),carry:c[3],review:c[4],followup:""},id,key));
      known.add(key);saved.push(target);
    }
  }
  return saved;
}
async function organizeExisting(tp) {
  const vault=tp.app.vault,helper=vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);
  const engine=new Function(await vault.read(helper))();let count=0;
  if(tp.file.path(true).startsWith(`${BASE}/Matchups/`))throw new Error("Run organization from the dashboard to avoid Templater overwriting the matchup.");
  for(const file of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))){
    const original=await vault.read(file);if(!original.startsWith("---")||properties(original,tp.obsidian.parseYaml).data.type!=="cfb-matchup")continue;
    await migrate(tp.app,tp.obsidian,file.path);
    const next=engine.organize(engine.cleanDisplay(original),tp.obsidian.parseYaml);
    await vault.process(file,current=>{if(current!==original)throw new Error("Matchup changed while organizing; retry.");return next;});count++;
  }
  new tp.obsidian.Notice(`Organized ${count} matchup(s). Original source evidence and notes preserved.`);
}
async function run(tp) {
  const active=tp.file.path(true);
  const edit=active.startsWith(`${BASE}/Observations/`)?active:null;
  if(edit)throw new Error("Use the Edit button in Reading view on the observation; run this template from the dashboard or matchup.");
  const action=await tp.system.suggester(["Add observation","Edit observation"],["add","edit"],false,"Observation action");if(!action)return;
  let observationPath;
  if(action==="edit") {
    const files=tp.app.vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Observations/`));
    observationPath=(await tp.system.suggester(files.map(f=>f.basename),files,false,"Choose observation"))?.path;
    if(!observationPath)return;
  }
  return open(tp.app,tp.obsidian,{observationPath,matchupPath:active.startsWith(`${BASE}/Matchups/`)?active:undefined});
}
return {open,run,serialize,properties,content,modal,norm,migrate,organizeExisting};
