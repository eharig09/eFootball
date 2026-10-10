<%*
const helper = tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/CFB/Views/cfb-edit.js");
if (!helper) throw new Error("Note editor has not synced yet. Wait for sync, then retry.");
await new Function(await tp.app.vault.read(helper))().run(tp);
%>
