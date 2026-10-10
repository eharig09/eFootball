<%*
const helper = tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/CFB/Views/cfb-create.js");
if (!helper) throw new Error("CFB helper has not synced yet. Wait for Drive sync, then retry.");
const factory = new Function(await tp.app.vault.read(helper))();
tR += await factory.matchup(tp);
%>