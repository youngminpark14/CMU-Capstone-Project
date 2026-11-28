# 0044. A document is matched to the next event of a series, and "bears on" means "is evidence for"

The Monitor is offered one candidate per series, its next event to close, and matches a document to the event it is evidence for, whether or not the document states the resolving figure.

## Context

The first paid backtest ran 56 news triggers through the pipeline and 54 ended `no_match`, so the trading simulation placed one order. Sixteen triggers came after the last stored event had closed. Of the 40 that reached the Monitor, it matched 2. Two things caused that.

The Monitor was told to match when the trigger "bears on exactly one" candidate, reading the rules "literally" and never guessing. It read that as: the document must state the figure or decision the event resolves on. Minutes, outlook speeches, jobs reports and the previous month's inflation release never do, and the release that does state the figure is published a minute after its event closes. The project's own sample episodes assume the opposite: hawkish minutes trigger a forecast of the next rate decision.

The five candidates were the open events most similar to the document. An exchange lists several months and meetings of a series at once, and their texts are nearly the same, so in early July four inflation months took four of the five places and the nearest rate decision was not offered for a governor's speech on monetary policy.

## Decision

The events search orders open events by their place in their series, the next to close first, then by similarity ([index.py](../../src/pmfa/knowledge/index.py)), and the match step keeps one candidate per series, at most `retrieval.event_candidates` ([pipeline.py](../../src/pmfa/episode/pipeline.py)). On the one re-match after a rules mismatch the excluded event is left out, so its series offers the event after it. The Monitor's instruction says what "bears on" means: the trigger is evidence about how the event will resolve, with examples, and a document on another subject bears on none ([specs.py](../../src/pmfa/models/roles/specs.py)). It answers with the candidate the trigger bears on most directly.

## Consequences

A document is forecast against the event it is most timely for, and a series cannot crowd another out of the candidates. More news triggers reach the forecast step, so the trading simulation costs more and says more. The specification's "top five candidate events" is now at most five, one per series. The change was made after the first paid run showed the matching step refusing almost everything; no trading result of a matched episode had been seen, and the scoring pass does not use the match step. The evaluation page gives the numbers before and after.

## Origin

Build, November 2026. The first paid backtest (2026-11-28) showed the matching step refusing almost every document. Checkpoint 3.1's markets collection took the top five by cosine similarity and had the judge confirm the match; the specification's pipeline step 2 ("Match") kept the top five. Here the five are one per series.
