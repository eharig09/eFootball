---
type: cfb-writing-desk
season: 2026
---

# CFB Writing Desk

[[06 Sports Betting/Football/CFB/CFB Dashboard|Dashboard]] · [[06 Sports Betting/Football/CFB/CFB Writing Pipeline.base|Edit article pipeline]] · [[06 Sports Betting/Football/CFB/CFB Observations.base|Edit triggers and predictions]]

Choose one reader question per article. In the matchup, write the **Decision brief**, then expand **Blog draft** to write in normal prose. Set article_status in Properties or the pipeline grid: idea → outline → draft → published → reviewed. Mark publishable on observations you want to use; this selects evidence for writing and does not publish it.

## Next checks

```dataview
TABLE team AS Team, matchup AS Matchup, next_check AS "Next check", implication AS Implication
FROM "06 Sports Betting/Football/CFB/Observations"
WHERE type = "cfb-observation" AND (!this.season OR season = this.season) AND next_check AND !resolution AND carry != "retired" AND carry != "superseded"
SORT next_check ASC
```

## Unresolved predictions

```dataview
TABLE matchup AS Matchup, prediction AS Prediction, probability AS "%", actual AS Actual
FROM "06 Sports Betting/Football/CFB/Observations"
WHERE type = "cfb-observation" AND (!this.season OR season = this.season) AND prediction AND (!prediction_result OR prediction_result = "unresolved")
SORT game_date ASC
```

## Article queue

```dataview
TABLE article_status AS Stage, reader_question AS "Reader question", central_argument AS Argument, publish_by AS Target, published_url AS Published
FROM "06 Sports Betting/Football/CFB/Matchups"
WHERE type = "cfb-matchup" AND (!this.season OR season = this.season) AND article_status != "reviewed"
SORT publish_by ASC, game_date ASC
```

## Selected evidence

```dataview
TABLE team AS Team, matchup AS Matchup, observation AS Observation, implication AS Implication
FROM "06 Sports Betting/Football/CFB/Observations"
WHERE type = "cfb-observation" AND (!this.season OR season = this.season) AND publishable = true
SORT game_date DESC
```

## Weekly writing rhythm

1. Research: record sourced evidence and state when each insight applies or fails.
2. Pregame: write a thesis, counterargument, and explicit market conditions; add observable predictions. Optional probabilities use 0–100% for clearly defined binary events.
3. Freeze: use **CFB Freeze Pregame Template** from the dashboard before kickoff.
4. Review: record actual outcomes, assess the mechanism separately from win/loss, and update carry-forward status.
5. Write: use the pregame and postgame outlines in the matchup. Verify selected evidence and record any publication URL manually.

Imports supply evidence. Your judgments, predictions, article text, and publication stages remain yours to enter.
