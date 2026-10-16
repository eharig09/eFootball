const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
for(const league of ['CFB','NFL']){
  const id=league.toLowerCase(),base=`06 Sports Betting/Football/${league}`;
  const source=fs.readFileSync(path.join(__dirname,'../vault',base,'Views',`${id}-observation-view.js`),'utf8');
  test(`${league} Add and Edit buttons use Templater's host API when Electron require fails`,async()=>{
    const events=[],messages=[],calls=[],hostAPI={Modal:class{},Setting:class{},parseYaml(){}};
    let apiReads=0;const app={plugins:{plugins:{'templater-obsidian':{templater:{functions_generator:{additional_functions(){apiReads++;return {obsidian:hostAPI};}}}}}},
      testOpen(api,options){calls.push({api,options});},vault:{getAbstractFileByPath:p=>({path:p}),read:async()=> 'return {open:async(app,api,options)=>app.testOpen(api,options)};'}};
    const matchup=base+'/Matchups/example.md',observation=base+'/Observations/example.md';
    let current={type:`${id}-matchup`,file:{path:matchup}};
    const dv={current:()=>current,pages:()=>[{type:`${id}-observation`,matchup,observation:'Insight',file:{path:observation}}],date:()=>({toFormat:()=> '2026-10-10'}),fileLink:p=>p,paragraph:s=>messages.push(s),container:{createEl:(_,{text})=>({addEventListener:(_,fn)=>events.push({text,fn})})}};
    const require=()=>{throw new Error("Cannot find module 'obsidian'");};
    await new AsyncFunction('dv','input','app','require',source)(dv,{side:'away',category:'offense'},app,require);
    await events.find(e=>e.text==='+ Add observation').fn();assert.equal(calls[0].api,hostAPI);assert.deepEqual(calls[0].options,{matchupPath:matchup,teamPath:undefined,side:'away',category:'offense'});
    events.length=0;await new AsyncFunction('dv','input','app','require',source)(dv,{},app,require);
    await events.find(e=>e.text==='Edit').fn();assert.equal(calls[1].options.observationPath,observation);
    events.length=0;current={type:`${id}-observation`,file:{path:observation}};
    await new AsyncFunction('dv','input','app','require',source)(dv,{editOnly:true},app,require);
    await events.find(e=>e.text==='Edit observation').fn();assert.equal(calls[2].options.observationPath,observation);assert.equal(apiReads,3);assert(!messages.some(s=>s.includes('Could not open form')));
  });
  test(`${league} missing host API gives an actionable error without opening or writing`,async()=>{
    let click,reads=0;const messages=[];const app={vault:{getAbstractFileByPath:p=>({path:p}),read:async()=>{reads++;throw new Error('Should not load helper');}}};
    const dv={current:()=>({type:`${id}-matchup`,file:{path:base+'/Matchups/example.md'}}),pages:()=>[],paragraph:s=>messages.push(s),container:{createEl:()=>({addEventListener:(_,fn)=>click=fn})}};
    await new AsyncFunction('dv','input','app',source)(dv,{},app);await click();assert.equal(reads,0);assert(messages.some(s=>s.includes('Enable Templater')));
  });
}
