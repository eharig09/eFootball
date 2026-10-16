<%*
const file=tp.app.vault.getAbstractFileByPath("06 Sports Betting/Football/NFL/Views/nfl-film.js");
if(!file)throw new Error("Game-note helper has not synced yet.");
await new Function(await tp.app.vault.read(file))().run(tp);
%>