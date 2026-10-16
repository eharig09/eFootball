const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const ROOT=path.join(__dirname,'../vault'),BASE='06 Sports Betting/Football/NFL';
const engine=new Function(fs.readFileSync(path.join(ROOT,BASE,'Views/nfl-engine.js'),'utf8'))();
const obs=new Function(fs.readFileSync(path.join(ROOT,BASE,'Views/nfl-observations.js'),'utf8'))();
function parseYaml(text){const result={};for(const line of text.split('\n')){const m=line.match(/^([a-z_]+):\s*(.*)$/);if(!m)continue;let value=m[2];try{value=JSON.parse(value)}catch{if(!value)value=null;}result[m[1]]=value;}return result;}
const meta=text=>parseYaml(text.match(/^---\n([\s\S]*?)\n---/)[1]);
function fixture(){return {game:{game_id:'2099_05_CHI_GB',season:2099,week:5,game_date:'2099-10-11',game_time:'13:00',home_team:'GNB',away_team:'CHI',stadium:'Lambeau Field',neutral_site:0,completed:0,updated_at:'2099-10-09T10:00:00Z'},
  home_identity:{abbreviation:'GNB',name:'Green Bay Packers',conference:'NFC',division:'NFC North'},away_identity:{abbreviation:'CHI',name:'Chicago Bears',conference:'NFC',division:'NFC North'},
  market:{line:3.5,total:47.5,home_moneyline:-175,away_moneyline:150,home_spread_odds:-108,away_spread_odds:-112,over_odds:-115,under_odds:-105},
  baseline_season:2098,profiles:{GNB:{epa_per_play:0.21357,defensive_epa_allowed:-0.10512,success_rate:0.512,defensive_success_allowed:0.4,explosive_rate:0.2,defensive_explosive_allowed:0.15},CHI:{epa_per_play:0.1,defensive_epa_allowed:0.05}},
  game_shape:{situational:{GNB:{games:17,seconds_per_play:28.1234,neutral_pass_rate:0.6},CHI:{games:17,seconds_per_play:30,neutral_pass_rate:0.55}}},
  football_lab:{football_lab:{away_points:21.321,home_points:26.779,margin_model:'test-v1',total_model:'core'},components:{home:{drives:11.111,plays_per_drive:6.123},away:{drives:10.321,plays_per_drive:6.4}},thin_sample:false},
  situation:[{label:'GNB rest',value:'7 days',detail:'Previous: road game'}],availability:{GNB:{rows:[{injury_id:'123',player_name:'Example QB',position:'QB',status:'Questionable',report_date:'2099-10-09T10:00:00Z',short_comment:'Limited practice',long_comment:'Review game status before kickoff',source_url:'https://example.test/injury'}]},CHI:{rows:[]}},
  content:[{content_id:99,title:'Chicago Bears at Green Bay Packers preview',canonical_url:'https://example.test/story',source_name:'Example source',content_type:'reporting',published_at:'2099-10-09T10:00:00Z',body_text:'Complete supplied evidence.',games:[{game_id:'2099_05_CHI_GB'}]}],line_movement:{provider:'Different bookmaker',spread_move:1}};}
const paths={home:BASE+'/Teams/Green Bay Packers.md',away:BASE+'/Teams/Chicago Bears.md'};
function render(raw=fixture(),old){const p=engine.normalizeNFL(raw,raw.game.game_id);return engine.refresh(old||engine.emptyNote(p.game,paths,'2099-10-11'),p,p.situation?.lines.providers[0],paths,'2099-10-10T10:00:00Z','https://engine.test',parseYaml);}
function runtime(raw=fixture()){
 const files=new Map();for(const p of ['Views/nfl-engine.js','Views/team-page.md','Views/engine-config.json'])files.set(BASE+'/'+p,fs.readFileSync(path.join(ROOT,BASE,p),'utf8'));
 const f=p=>({path:p,basename:path.basename(p,'.md')});const vault={getAbstractFileByPath:p=>files.has(p)?f(p):null,getMarkdownFiles:()=>[...files.keys()].filter(p=>p.endsWith('.md')).map(f),read:async f=>files.get(f.path),createFolder:async p=>files.set(p,''),create:async(p,s)=>{assert(!files.has(p));files.set(p,s);return f(p);},process:async(f,fn)=>files.set(f.path,fn(files.get(f.path)))};
 const requests=[];let phase=0;
 const tp={hooks:{on_all_templates_executed:()=>{}},app:{vault,workspace:{getLeaf:()=>({openFile:async()=>{}})}},file:{path:()=>BASE+'/NFL Dashboard.md'},system:{prompt:async()=>raw.game.game_id,suggester:async(_labels,values)=>values[0]},obsidian:{parseYaml,Notice:class{},requestUrl:async({url,method})=>{requests.push({url,method});return {status:200,json:raw};}}};return {tp,files,requests};
}
test('NFL IDs are strings; CFB links, wrong hosts, and malformed identifiers are rejected',()=>{
 for(const input of ['2099_05_CHI_GB','https://engine.test/nfl/games/2099_05_CHI_GB/','https://engine.test/api/v1/nfl/games/2099_05_CHI_GB'])assert.equal(engine.parseGameInput(input,'https://engine.test'),'2099_05_CHI_GB');
 for(const input of ['123','https://engine.test/college-football/games/123/','https://other.test/nfl/games/2099_05_CHI_GB/','../unsafe'])assert.throws(()=>engine.parseGameInput(input,'https://engine.test'));
});
test('Eastern kickoff handles standard time, daylight time, and invalid times',()=>{
 assert.equal(engine.kickoffEastern('2026-10-11','09:30'),'2026-10-11T13:30:00.000Z');assert.equal(engine.kickoffEastern('2027-01-10','13:00'),'2027-01-10T18:00:00.000Z');
 assert.throws(()=>engine.kickoffEastern('2026-02-30','13:00'));assert.throws(()=>engine.kickoffEastern('2026-10-11','25:00'));
});
test('NFL adaptation validates identity and keeps legacy ID aliases separate from canonical teams',()=>{
 const p=engine.normalizeNFL(fixture(),'2099_05_CHI_GB');assert.equal(p.game.home_team_id,'GNB');assert.equal(p.game.home_team,'Green Bay Packers');
 assert.throws(()=>engine.normalizeNFL(fixture(),'2099_05_CHI_DET'));const bad=fixture();bad.home_identity.abbreviation='GB';assert.throws(()=>engine.normalizeNFL(bad,bad.game.game_id));
});
test('NFL home-spread conversion keeps exact prices and unknown sportsbook provenance',()=>{
 const text=render(),m=meta(text);assert.equal(m.line,-3.5);assert.equal(m.home_spread_price,-108);assert.equal(m.away_spread_price,-112);assert.equal(m.over_price,-115);assert.equal(m.price,null);
 assert.equal(m.sportsbook,'nflverse schedule (book unspecified)');assert(!m.sportsbook.includes('Different bookmaker'));
 const zero=fixture();zero.market.line=0;assert.equal(meta(render(zero)).line,0);
});
test('NFL compact metrics label EPA, prior season basis, injury status, and full source evidence',()=>{
 const text=render();assert(text.includes('EPA / play'));assert(text.includes('0.21'));assert(text.includes('Season | 2098'));assert(text.includes('prior-season fallback'));assert(text.includes('17 games · 2098 season')); assert(text.includes('Complete supplied evidence.'));
 assert(text.includes('Review game status before kickoff'));assert(text.includes('Reported status, not a confirmed game inactive'));assert(text.includes('NFL Writing')===false);
 assert(!engine.block(text,'engine-home-context').content.replace(/<!--[^\n]*-->/g,'').includes('T10:00:00Z'));assert(text.includes('## Decision brief'));assert(text.includes('## Predictions and review'));assert(text.includes('Blog draft'));
 const international=fixture();international.game.stadium='Tottenham Hotspur Stadium';assert(render(international).includes('International venue'));
});
test('NFL refresh preserves authored analysis, quote and expired evidence; missing sections remain flagged',()=>{
 let original=render().replace('- Thesis:','- Thesis: My authored thesis').replace('line: -3.5','line: -4');
 const raw=fixture();raw.content=[];raw.availability={};raw.market.line=7;const next=render(raw,original);
 assert.equal(meta(next).line,-4);assert.equal(meta(next).engine_line,-7);assert(next.includes('My authored thesis'));assert(next.includes('Complete supplied evidence.'));assert(next.includes('Review game status before kickoff'));
 delete raw.content;delete raw.availability;delete raw.market;const partial=render(raw,next);assert.equal(meta(partial).engine_import_status,'partial');assert(partial.includes('Complete supplied evidence.'));assert.equal(meta(partial).line,-4);
 const wrong=fixture();wrong.game.game_id='2099_05_CHI_DET';assert.throws(()=>render(wrong,original),/another engine game/);
});
test('NFL importer creates linked canonical teams, reuses pages, and makes only NFL GET requests',async()=>{
 const {tp,files,requests}=runtime();const target=await engine.run(tp);await engine.run(tp);assert(target.startsWith(BASE+'/Matchups/'));assert.equal([...files.keys()].filter(p=>p.startsWith(BASE+'/Matchups/')&&p.endsWith('.md')).length,1);
 assert.equal(meta(files.get(paths.home)).engine_team_id,'GNB');assert.equal(meta(files.get(paths.home)).division,'NFC North');assert.equal(requests.length,2);assert(requests.every(r=>r.method==='GET'&&r.url.includes('/api/v1/nfl/games/')&&!r.url.endsWith('/preview')));
});
test('NFL required packet failure writes nothing and snapshots freeze new observation fields',async()=>{
 const {tp,files}=runtime();tp.obsidian.requestUrl=async()=>({status:503,json:{}});const before=files.size;await assert.rejects(engine.run(tp));assert.equal(files.size,before);
 const second=runtime();const target=await engine.run(second.tp),game=meta(second.files.get(target));
 const observation=obs.serialize(game,{path:target},{observation:'Watch protection',side:'home',category:'offense',direction:'negative',prediction:'Allow at least 3 sacks',probability:65,applies_when:'Starting tackle out',invalidated_by:'Tackle cleared',publishable:true},'test');second.files.set(BASE+'/Observations/test.md',observation);
 const frozen=await engine.freeze(second.tp),text=second.files.get(frozen);assert(text.includes('Starting tackle out'));assert(text.includes('probability: 65'));assert(text.includes('publishable: true'));assert(!text.includes('```dataviewjs'));assert.equal(meta(text).type,'nfl-pregame-snapshot');
});


test('NFL current slate picker uses the season API and unfinished matchups',async()=>{
  const raw=fixture(),r=runtime(raw);r.tp.system.prompt=async label=>label.startsWith('NFL season')?'2099':'';
  r.tp.obsidian.requestUrl=async({url})=>({status:200,json:url.includes('?season=')?{matchups:[{game_id:raw.game.game_id,completed:false,date:'2099-10-11',week:5,sides:[{role:'away',name:'Chicago Bears'},{role:'home',name:'Green Bay Packers'}]},{game_id:'2099_04_CHI_GB',completed:true}]}:raw});
  const result=await engine.run(r.tp);assert(result.endsWith('Chicago Bears at Green Bay Packers.md'));
});
