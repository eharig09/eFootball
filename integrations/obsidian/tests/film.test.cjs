const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function parseYaml(raw){const out={};for(const line of raw.split('\n')){const m=line.match(/^([a-z_]+):\s*(.*)$/);if(!m)continue;try{out[m[1]]=JSON.parse(m[2])}catch{out[m[1]]=m[2]||null;}}return out;}
for(const league of ['CFB','NFL']){
 const low=league.toLowerCase(),base=`06 Sports Betting/Football/${league}`,root=path.join(__dirname,'../vault',base);
 const source=fs.readFileSync(path.join(root,'Views',`${low}-film.js`),'utf8'),h=new Function(source)();
 const game={season:2026,week:5,game_date:'2026-10-10',away_team:`[[${base}/Teams/Away]]`,home_team:`[[${base}/Teams/Home]]`};
 const raw=`---\ntype: ${low}-matchup\n${Object.entries(game).map(([k,v])=>`${k}: ${JSON.stringify(v)}`).join('\n')}\nengine_api_url: "https://engine.test"\nengine_game_id: "${league==='CFB'?'123':'2026_05_AWY_HOM'}"\n---\n\nUser's original research\n`;
 function runtime(){const files=new Map([[base+'/Matchups/source.md',raw],[base+'/Games/Index.md','Unrelated index'],[base+`/Views/${low}-engine.js`,fs.readFileSync(path.join(root,'Views',`${low}-engine.js`),'utf8')]]);const file=p=>({path:p,basename:path.basename(p,'.md')});const vault={getAbstractFileByPath:p=>files.has(p)?file(p):null,getMarkdownFiles:()=>[...files.keys()].filter(p=>p.endsWith('.md')).map(file),read:async f=>files.get(f.path),createFolder:async p=>files.set(p,''),create:async(p,s)=>{assert(!files.has(p));files.set(p,s);},process:async(f,fn)=>files.set(f.path,fn(files.get(f.path)))};return {files,tp:{app:{vault},obsidian:{parseYaml}}};}
 test(`${league} game prose is grouped by side/category, preserves multiline notes and ignores fenced headings`,()=>{
   const text=h.gameBody(game).replace('### Offense\n\n- ','### Offense\n\n- + Pressure on third down\n  Q2 08:14; replacement guard\n\n```text\n## Home team — fake\n### Defense\n```');
   const s=h.sections(text);assert(s.away.offense.includes('replacement guard'));assert(s.away.offense.includes('## Home team — fake'));assert(!h.hasNotes(s.home.offense));assert(h.hasNotes(s.away.offense));assert(!text.includes('price:'));assert(!text.includes('Add observation'));
 });
 test(`${league} reopening/refreshing a source reuses one game note and never overwrites prose or old research`,async()=>{
   const {tp,files}=runtime();const first=await h.ensure(tp,base+'/Matchups/source.md');files.set(first,files.get(first).replace('### Defense\n\n- ','### Defense\n\n- My tape observation'));
   const before=files.get(first);assert.equal(await h.ensure(tp,base+'/Matchups/source.md'),first);assert.equal(files.get(first),before);assert.equal(files.get(base+'/Matchups/source.md'),raw);assert.equal([...files.keys()].filter(p=>p.startsWith(base+'/Games/')&&p!==base+'/Games/Index.md').length,1); // one game note
   assert(files.get(first).includes(league==='CFB'?'/college-football/games/123/':'/nfl/games/2026_05_AWY_HOM/'));
 });
 test(`${league} team-page upgrade preserves Properties and custom writing without duplicating on repeat`,()=>{
   const stock='---\ntype: '+low+'-team\nteam_name: __TEAM_JSON__\nseason: __SEASON__\n---\n\n# __TEAM_NAME__\n\nGenerated view\n';
   const original=stock.replace('__TEAM_JSON__','"Home"').replace('__SEASON__','2026').replace('__TEAM_NAME__','Home').replace('Generated view','My authored current assessment');
   const upgraded=h.upgradeTeam(original,stock,parseYaml);assert(upgraded.includes('My authored current assessment'));assert.equal(h.properties(upgraded,parseYaml).data.team_name,'Home');assert.equal(h.upgradeTeam(upgraded,stock,parseYaml),upgraded);
 });


 test(`${league} manual game is a normal note, reopens without overwriting, and later accepts a site link without losing tape notes`,async()=>{
   const {tp,files}=runtime();for(const name of ['Away','Home'])files.set(`${base}/Teams/${name}.md`,`---\ntype: ${low}-team\nteam_name: "${name}"\nseason: 2026\n---\n`);
   tp.date={now:()=> '2026-10-10'};tp.system={prompt:async label=>label.startsWith('Game date')?'2026-10-10':'2026',suggester:async(_labels,values,_required,title)=>values[title==='Away team'?0:1]};
   const target=await h.manual(tp);files.set(target,files.get(target).replace('### Offense\n\n- ','### Offense\n\n- Original film read'));const before=files.get(target);
   assert.equal(await h.manual(tp),target);assert.equal(files.get(target),before);assert.equal(await h.ensure(tp,base+'/Matchups/source.md'),target);assert(files.get(target).includes('Original film read'));assert(h.properties(files.get(target),parseYaml).data.source_matchup);
 });
 test(`${league} cancelling manual creation writes nothing`,async()=>{
   const {tp,files}=runtime();tp.date={now:()=> '2026-10-10'};tp.system={prompt:async()=>null};const before=files.size;await assert.rejects(h.manual(tp),/Cancelled/);assert.equal(files.size,before);
 });
 test(`${league} game command imports the requested historical ID, skips duplicate source opening, and opens the prose note`,async()=>{
   const {tp,files}=runtime(),hooks=[],opened=[];let prompts=0;
   tp.file={path:()=>base+'/Dashboard.md'};tp.hooks={on_all_templates_executed:fn=>hooks.push(fn)};tp.app.workspace={openLinkText:p=>opened.push(p)};
   tp.system={prompt:async()=>{prompts++;return 'past-game-id';},suggester:async(_labels,values,_required,title)=>title==="Game notes"?"import":values[0]};
   files.set(base+`/Views/${low}-engine.js`,`return {run:async(tp)=>{tp.app.pasted=await tp.system.prompt("Paste engine game URL or ID.");tp.app.quote=await tp.system.suggester(["quote"],["supplied quote"],true,"Choose sportsbook");tp.hooks.on_all_templates_executed(()=>tp.app.workspace.openLinkText("WRONG SOURCE VIEW"));return ${JSON.stringify(base+'/Matchups/source.md')};}};`);
   const result=await h.run(tp);for(const fn of hooks)await fn();assert.equal(prompts,1);assert.equal(tp.app.pasted,'past-game-id');assert.equal(tp.app.quote,'supplied quote');assert.deepEqual(opened,[result]);assert(result.startsWith(base+'/Games/'));
 });
 test(`${league} site comparison stays folded, shows its import date, and never substitutes for tape prose`,async()=>{
   const {tp,files}=runtime(),target=await h.ensure(tp,base+'/Matchups/source.md');
   files.set(base+`/Views/${low}-film.js`,source);files.set(base+'/Matchups/source.md',raw+`<!-- ${low}:engine-brief:start -->\n## Matchup evidence\nSite metrics, not my tape opinion\n<!-- ${low}:engine-brief:end -->`);
   tp.app.metadataCache={getFileCache:()=>({frontmatter:{engine_imported_at:'2026-10-10T10:00:00Z'}})};
   const messages=[],dv={current:()=>h.properties(files.get(target),parseYaml).data,paragraph:s=>messages.push(s),fileLink:p=>p};
   const before=files.get(target);await new AsyncFunction('dv','input','app',fs.readFileSync(path.join(root,'Views',`${low}-film-view.js`),'utf8'))(dv,{mode:'compare'},tp.app);
   assert(messages.some(s=>s.includes('> [!abstract]- Site comparison · imported 2026-10-10')));assert(messages.some(s=>s.includes('retrospective')));assert.equal(files.get(target),before);
 });
 test(`${league} team compilation combines games with context, excludes opposing/other-season notes, and keeps legacy provenance separate`,async()=>{
   const {tp,files}=runtime();const p=await h.ensure(tp,base+'/Matchups/source.md');const text=files.get(p).replace('## Game context\n\n','## Game context\n\nTrailing by two scores; partial tape\n\n').replace('### Offense\n\n- ','### Offense\n\n- AWAY ONLY').replace('## Home team — Home\n\n### Offense\n\n- ','## Home team — Home\n\n### Offense\n\n- HOME INSIGHT');files.set(p,text);
   files.set(base+`/Views/${low}-film.js`,source);
   const pages=[{...game,type:`${low}-game`,file:{path:p}},{...game,season:2025,type:`${low}-game`,file:{path:'OLD GAME'}}],legacy={type:`${low}-observation`,team:game.home_team,season:2026,observation:'Earlier research',matchup:base+'/Matchups/another-game',opponent:game.away_team,file:{path:'legacy.md'}};
   const second={...game,game_date:'2026-10-03',away_team:game.home_team,home_team:`[[${base}/Teams/Other opponent]]`},secondPath=base+'/Games/second.md';
   files.set(secondPath,h.gameBody(second).replace('### Offense\n\n- ','### Offense\n\n- SECOND GAME INSIGHT'));pages.push({...second,type:`${low}-game`,file:{path:secondPath}});
   const output=[];const dv={current:()=>({type:`${low}-team`,season:2026,engine_team_id:'HOM',file:{path:base+'/Teams/Home.md'}}),pages:q=>q.includes('/Games')?pages:[legacy],paragraph:s=>output.push(s),header:(_,s)=>output.push(s),fileLink:p=>p};
   await new AsyncFunction('dv','input','app',fs.readFileSync(path.join(root,'Views',`${low}-film-view.js`),'utf8'))(dv,{mode:'team'},tp.app);
   assert(output.join('\n').includes('HOME INSIGHT'));assert(output.join('\n').includes('SECOND GAME INSIGHT'));assert(output.join('\n').includes('Other opponent')); assert(output.join('\n').includes('partial tape'));assert(!output.join('\n').includes('AWAY ONLY'));assert(!output.join('\n').includes('OLD GAME'));assert(output.join('\n').includes('another-game'));assert(output.join('\n').includes('original game associations preserved'));
 });
}
