// Form editor for the existing Markdown-backed observations. No extra plugin required.
const BASE = "06 Sports Betting/Football/CFB";
function entries(text, engine) {
  const found=[];
  for(const side of ["away","home"]) for(const category of ["offense","defense","context"])
    for(const imported of [false,true]) {
      const id=`${imported?"engine-":""}${side}-${category}`;
      const section=engine.block(text,id); if(!section)continue;
      section.content.split(/\r?\n/).forEach((line,index)=>{
        if(!line.trim().startsWith("|"))return;
        const cells=engine.splitCells(line);
        if(cells[0]==="+"||/^:?-+:?$/.test(cells[0])||!cells.slice(0,3).some(Boolean))return;
        if(cells.length!==(imported?6:5))throw new Error(`Unexpected columns in ${id}. Repair the table first.`);
        found.push({id,index,cells,side,category,imported});
      });
    }
  return found;
}
function saveRow(text, engine, row, values) {
  const id=row?.id || `${values.side}-${values.category}`;
  const section=engine.block(text,id); if(!section)throw new Error("Section markers are missing.");
  const lines=section.content.split(/\r?\n/);
  const cells=[values.plus,values.minus,row?.imported?row.cells[2]:values.evidence,values.carry,values.review];
  if(!cells.slice(0,3).some(v=>String(v||"").trim()))throw new Error("Enter an observation or evidence.");
  if(row?.imported)cells.push(row.cells[5]);
  const replacement=`| ${cells.map(engine.cell).join(" | ")} |`;
  if(row) {
    if(JSON.stringify(engine.splitCells(lines[row.index]))!==JSON.stringify(row.cells))throw new Error("Row changed; reopen the editor.");
    lines[row.index]=replacement;
  } else {
    const empty=lines.findIndex(line=>line.trim().startsWith("|") && engine.splitCells(line).length===5 &&
      !engine.splitCells(line).slice(0,3).some(Boolean));
    if(empty>=0)lines[empty]=replacement; else lines.push(replacement);
  }
  return engine.replaceBlock(text,id,lines.join("\n"));
}
function form(tp, initial, imported, names) {
  const {Modal,Setting,Notice}=tp.obsidian;
  return new Promise(resolve=>{
    class NoteForm extends Modal {
      onOpen() {
        const values={...initial}; const el=this.contentEl;
        el.createEl("h2",{text:imported?"Assess imported evidence":"Edit team observation"});
        el.createEl("p",{text:imported?"Evidence stays linked to its engine item. Your direction and carry-forward edits survive refresh.":"Save an observation without editing Markdown table rows."});
        const dropdown=(label,key,options,disabled=false)=>new Setting(el).setName(label).addDropdown(c=>{
          for(const [value,name] of Object.entries(options))c.addOption(value,name);
          if(values[key]&&!Object.hasOwn(options,values[key]))c.addOption(values[key],values[key]);
          c.setValue(values[key]).setDisabled(disabled).onChange(v=>values[key]=v);
        });
        dropdown("Team","side",names,!!initial.fixed);
        dropdown("Section","category",{offense:"Offense",defense:"Defense",context:"Context"},!!initial.fixed);
        for(const [key,label] of [["plus","+ Favorable observation"],["minus","− Unfavorable observation"],["evidence","Evidence / condition"]])
          new Setting(el).setName(label).addTextArea(c=>{
            c.setValue(values[key]||"").setDisabled(imported&&key==="evidence").onChange(v=>values[key]=v);
            c.inputEl.rows=key==="evidence"?5:2; c.inputEl.style.width="100%";
          });
        dropdown("Carry forward","carry",{"game-only":"This game only",watch:"Watch",active:"Active",retired:"Retired",superseded:"Superseded"});
        new Setting(el).setName("Review / expires").setDesc("YYYY-MM-DD or a condition, such as when the starter returns.")
          .addText(c=>c.setValue(values.review||"").onChange(v=>values.review=v));
        new Setting(el).addButton(c=>c.setButtonText("Cancel").onClick(()=>this.close()))
          .addButton(c=>c.setButtonText("Save note").setCta().onClick(()=>{
            if(![values.plus,values.minus,values.evidence].some(v=>String(v||"").trim())){new Notice("Enter an observation or evidence.");return;}
            resolve(values);this.close();
          }));
      }
      onClose(){this.contentEl.empty();resolve(null);}
    }
    new NoteForm(tp.app).open();
  });
}
async function run(tp) {
  if(tp.file.path(true).startsWith(`${BASE}/Matchups/`))
    throw new Error("Run CFB Edit Notes Template from the CFB Dashboard so Templater cannot overwrite the matchup.");
  const vault=tp.app.vault;
  const engineFile=vault.getAbstractFileByPath(`${BASE}/Views/cfb-engine.js`);
  if(!engineFile)throw new Error("Engine helper has not synced yet.");
  const engine=new Function(await vault.read(engineFile))();
  const candidates=[];
  for(const f of vault.getMarkdownFiles().filter(f=>f.path.startsWith(`${BASE}/Matchups/`))) {
    const raw=await vault.read(f),fm=raw.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if(fm&&tp.obsidian.parseYaml(fm[1])?.type==="cfb-matchup")candidates.push(f);
  }
  candidates.sort((a,b)=>b.basename.localeCompare(a.basename));
  if(!candidates.length){new tp.obsidian.Notice("Create or import a matchup first.");return;}
  const file=await tp.system.suggester(candidates.map(f=>f.basename),candidates,false,"Choose matchup");
  if(!file)return;
  const original=await vault.read(file),fm=tp.obsidian.parseYaml(original.match(/^---\r?\n([\s\S]*?)\r?\n---/)[1]);
  const names={};for(const side of ["away","home"])names[side]=String(fm[`${side}_team`]||side).replace(/^\[\[|\]\]$/g,"").split("/").pop().replace(/\.md$/,"");
  const action=await tp.system.suggester(["Add observation","Edit existing observation"],["add","edit"],false,"What would you like to do?");
  if(!action)return;
  let row=null;
  if(action==="edit") {
    const rows=entries(original,engine);
    if(!rows.length){new tp.obsidian.Notice("No observations yet. Use Add observation.");return;}
    row=await tp.system.suggester(rows.map(r=>`${names[r.side]} · ${r.category} · ${r.imported?"Imported":"Manual"} · ${r.cells.slice(0,3).filter(Boolean).join(" / ")}`),rows,false,"Choose observation");
    if(!row)return;
  }
  const values=await form(tp,row?{side:row.side,category:row.category,plus:row.cells[0],minus:row.cells[1],evidence:row.cells[2].replace(/<br>/g,"\n"),carry:row.cells[3],review:row.cells[4],fixed:true}:
    {side:"away",category:"offense",plus:"",minus:"",evidence:"",carry:"game-only",review:""},!!row?.imported,names);
  if(!values)return;
  const updated=saveRow(original,engine,row,values);
  await vault.process(file,current=>{if(current!==original)throw new Error("Matchup changed while editing. Reopen the form to save safely.");return updated;});
  new tp.obsidian.Notice("Observation saved. Team pages will pull the updated row.");
}
return {run,entries,saveRow,form};
