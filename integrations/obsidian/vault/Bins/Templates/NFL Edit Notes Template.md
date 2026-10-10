<%*
const helper = tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/NFL/Views/nfl-observations.js");
if (!helper) throw new Error("Observation helper has not synced yet.");
await new Function(await tp.app.vault.read(helper))().run(tp);
%>
