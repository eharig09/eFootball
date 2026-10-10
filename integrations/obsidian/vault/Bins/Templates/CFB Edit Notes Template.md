<%*
const helper = tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/CFB/Views/cfb-observations.js");
if (!helper) throw new Error("Observation helper has not synced yet.");
await new Function(await tp.app.vault.read(helper))().run(tp);
%>
