const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
function parseYaml(raw){const p={};for(const l of raw.split('\n')){const m=l.match(/^([a-z_]+):\s*(.*)$/);if(m){try{p[m[1]]=JSON.parse(m[2])}catch{p[m[1]]=m[2]||null;}}}return p;}
for(const league of ['CFB','NFL']){
 const low=league.toLowerCase(),nfl=league==='NFL',base=`06 Sports Betting/Football/${league}`,root=path.join(__dirname,'../vault',base);
 const h=new Function(fs.readFileSync(path.join(root,'Views',`${low}-film.js`),'utf8'))(),engine=new Function(fs.readFileSync(path.join(root,'Views',`${low}-engine.js`),'utf8'))();
 const team={id:nfl?'DET':1,name:nfl?'Detroit Lions':'Selected U'},other=nfl?'CHI':2;
 function packet(){return nfl?{season:2025,team:{abbreviation:'DET'},schedule:{rows:[{date:'2025-09-01',date_url:'/nfl/games/2025_01_CHI_DET/#review',week:1,home:'DET',away:'CHI',opponent:'CHI',result:'W',score:'31–0',home_score:31,away_score:0},{date:'2025-09-08',date_url:'/nfl/games/2025_02_DET_CHI/#overview',week:2,home:'CHI',away:'DET',opponent:'CHI',result:null,score:null}]}}:{season:2025,team:{team_id:1},schedule:[{game_id:111,week:1,start_date:'2025-09-01T14:00:00Z',home_team_id:1,away_team_id:2,home_team:'Selected U',away_team:'Other U',completed:true,home_points:0,away_points:14},{game_id:222,week:2,start_date:'2025-09-08T14:00:00Z',home_team_id:2,away_team_id:1,home_team:'Other U',away_team:'Selected U',completed:false}]};}
 function runtime(){const files=new Map([[base+`/Views/${low}-engine.js`,fs.readFileSync(path.join(root,'Views',`${low}-engine.js`),'utf8')],[base+'/Views/engine-config.json','{"api_url":"https://engine.test"}']]),requests=[],menus=[];
  const vault={getAbstractFileByPath:p=>files.has(p)?{path:p,basename:path.basename(p,'.md')}:null,getMarkdownFiles:()=>[...files.keys()].filter(p=>p.endsWith('.md')).map(p=>({path:p,basename:path.basename(p,'.md')})),read:async f=>files.get(f.path)};
  const tp={app:{vault},system:{prompt:async()=> '2025',suggester:async(labels,values,_req,title)=>{menus.push({labels,title});return title.startsWith("Choose a team")?values.find(v=>String(v.id)===String(team.id)):values[0];}},obsidian:{parseYaml,requestUrl:async({url,method})=>{requests.push({url,method});return {status:200,json:url.includes('/teams/')?packet():{teams:nfl?[{abbreviation:'DET',name:'Detroit Lions'},{abbreviation:'CHI',name:'Chicago Bears'}]:[{team_id:1,school:'Selected U'}]}};}}};return {tp,files,requests,menus};}
 test(`${league} selector includes past and upcoming games, both roles, zero scores, and historical seasons`,async()=>{
   const games=h.scheduleGames(packet(),team,2025,'https://engine.test',engine);assert.equal(games.length,2);assert.equal(games[0].complete,true);assert.equal(games[1].complete,false);assert.equal(games[0].isHome,true);assert.equal(games[1].isHome,false);assert(games[0].score.endsWith('0')||games[0].score.startsWith('0'));
   const {tp,files,requests,menus}=runtime(),before=files.size,id=await h.selectGame(tp);assert.equal(id,nfl?'2025_01_CHI_DET':'111');assert(requests.some(r=>r.url.endsWith('?season=2025')));assert(requests.every(r=>r.method==='GET'));assert.equal(files.size,before);assert(menus.at(-1).labels.some(s=>s.includes('Final')));assert(menus.at(-1).labels.some(s=>s.includes('Scheduled')));
 });
 test(`${league} selector rejects schedules from another team/season and unsafe IDs before notes are written`,()=>{
   const wrong=packet();wrong.season=2024;assert.throws(()=>h.scheduleGames(wrong,team,2025,'https://engine.test',engine),/another team or season/);
   const row=packet();if(nfl)row.schedule.rows[0].date_url='https://outside.test/nfl/games/2025_01_CHI_DET/';else row.schedule[0].game_id='../bad';assert.throws(()=>h.scheduleGames(row,team,2025,'https://engine.test',engine));
   const otherPacket=packet();if(nfl)otherPacket.schedule.rows[0].home='SEA';else otherPacket.schedule[0].home_team_id=99;assert.throws(()=>h.scheduleGames(otherPacket,team,2025,'https://engine.test',engine),/another team/);
 });
 test(`${league} cancellation and failed schedule fetch leave files unchanged`,async()=>{
   const {tp,files}=runtime(),before=files.size;tp.system.suggester=async()=>null;assert.equal(await h.selectGame(tp),null);assert.equal(files.size,before);
   const failed=runtime();failed.tp.obsidian.requestUrl=async()=>({status:503,json:{}});await assert.rejects(h.selectGame(failed.tp),/HTTP 503/);assert.equal(failed.files.size,before);
 });
 test(`${league} selected existing game reopens without refreshing or duplicating authored prose`,async()=>{
   const {tp,files}=runtime(),id=nfl?'2025_01_CHI_DET':'111',sourcePath=base+'/Matchups/source.md',gamePath=base+'/Games/existing.md';
   files.set(sourcePath,`---\ntype: ${low}-matchup\nengine_game_id: "${id}"\n---\n`);files.set(gamePath,`---\ntype: ${low}-game\nsource_matchup: "[[${sourcePath.slice(0,-3)}]]"\n---\n\nMy authored tape notes`);
   const hooks=[],opens=[];tp.file={path:()=>base+'/Dashboard.md'};tp.hooks={on_all_templates_executed:fn=>hooks.push(fn)};tp.app.workspace={openLinkText:p=>opens.push(p)};const original=tp.system.suggester;tp.system.suggester=async(labels,values,req,title)=>title==='Game notes'?'select':original(labels,values,req,title);
   const before=new Map(files);assert.equal(await h.run(tp),gamePath);for(const fn of hooks)await fn();assert.deepEqual(opens,[gamePath]);assert.deepEqual(files,before);
 });
}
