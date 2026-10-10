<%*
const helper = tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/NFL/Views/nfl-engine.js");
if (!helper) throw new Error("Engine helper has not synced yet. Wait for sync, then retry.");
const engine = new Function(await tp.app.vault.read(helper))();
await engine.freeze(tp);
%>