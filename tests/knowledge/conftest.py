"""Small invented documents about Fed policy, CPI, and unrelated topics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pmfa.config import Config
from pmfa.knowledge.embedders import HashingEmbedder
from pmfa.knowledge.retriever import ChromaKnowledge
from pmfa.records.evidence import Document

AS_OF = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

FED_STATEMENT = """Recent indicators suggest that economic activity has continued to expand at a moderate pace. Job gains have slowed, and the unemployment rate has edged up but remains low. Inflation has eased over the past year but remains somewhat elevated.

The Committee seeks to achieve maximum employment and inflation at the rate of 2 percent over the longer run. In support of its goals, the Committee decided to maintain the target range for the federal funds rate at 3-3/4 to 4 percent.

In considering the extent and timing of additional adjustments to the target range, the Committee will carefully assess incoming data, the evolving outlook, and the balance of risks. The Committee will continue reducing its holdings of Treasury securities."""

FED_SPEECH = """Governor remarks on the outlook for monetary policy. Inflation has come down a long way from its peak, but the last stretch toward two percent is proving slow, and services inflation remains sticky.

I see the labor market as roughly in balance. Wage growth has moderated and openings have fallen back toward their level before the pandemic. With policy restrictive, I would support a further reduction in the federal funds rate if inflation data continue to cooperate.

The timing of any cut will depend on the data. I do not see a case for raising rates, and I will be watching the next consumer price report closely."""

WIRE_ARTICLE = """Several Federal Reserve officials signaled on Wednesday that they are prepared to lower interest rates at the December meeting, citing a cooling labor market and steady progress on inflation.

Speaking at separate events, two regional bank presidents said the case for a quarter point reduction in the federal funds rate had strengthened since the October meeting, when the central bank held rates steady.

Markets moved to price a higher chance of a cut. Traders in futures tied to the policy rate now see roughly even odds of a December move, up from about one in three a week ago.

One official cautioned that a cut was not guaranteed and that a hot inflation reading could change the outlook. The next consumer price index report is due later this month."""

CPI_RELEASE = """The Consumer Price Index for All Urban Consumers increased 0.3 percent on a seasonally adjusted basis in October, after rising 0.2 percent in September. Over the last 12 months, the all items index increased 2.9 percent before seasonal adjustment.

The index for shelter rose 0.4 percent in October and was the largest factor in the monthly increase. The energy index fell 0.5 percent, while the food index increased 0.2 percent.

The index for all items less food and energy rose 0.3 percent in October. The all items less food and energy index rose 3.1 percent over the last 12 months."""

GARDEN = """Tomato plants need full sun, well drained soil, and steady watering through the summer months. Mulch helps the soil hold moisture during dry spells.

Prune the lower leaves once the plants are established, and stake or cage them early so the stems do not snap under the weight of the fruit."""


def doc(
    doc_id: str,
    text: str,
    *,
    title: str = "",
    source: str = "newswire.example",
    published_at: datetime = AS_OF - timedelta(days=5),
    kind: str = "news",
) -> Document:
    return Document(
        doc_id=doc_id,
        source=source,
        kind=kind,
        url=f"https://{source}/{doc_id}",
        title=title or doc_id.replace("-", " ").title(),
        published_at=published_at,
        text=text,
    )


def corpus() -> list[Document]:
    """The shared corpus: two Fed documents, one wire story, a CPI release, a gardening note."""
    return [
        doc(
            "fomc-statement-2026-10",
            FED_STATEMENT,
            title="Federal Reserve issues FOMC statement",
            source="federalreserve.gov",
            published_at=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
            kind="fomc_statement",
        ),
        doc(
            "fed-speech-2026-11",
            FED_SPEECH,
            title="Governor speech on the economic outlook and monetary policy",
            source="federalreserve.gov",
            published_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
            kind="speech",
        ),
        doc(
            "wire-fed-cut",
            WIRE_ARTICLE,
            title="Fed officials signal openness to a December rate cut",
            published_at=datetime(2026, 11, 12, 14, 0, tzinfo=UTC),
        ),
        doc(
            "cpi-2026-10",
            CPI_RELEASE,
            title="Consumer Price Index, October 2026",
            source="bls.gov",
            published_at=datetime(2026, 11, 13, 13, 30, tzinfo=UTC),
            kind="cpi_release",
        ),
        doc(
            "garden-tomatoes",
            GARDEN,
            title="Growing tomatoes at home",
            source="gardening.example",
            published_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        ),
    ]


def config(**retrieval: Any) -> Config:
    return Config.model_validate({"retrieval": retrieval}) if retrieval else Config()


@pytest.fixture
def knowledge() -> Iterator[ChromaKnowledge]:
    k = ChromaKnowledge(config(), embedder=HashingEmbedder())
    yield k
    k.close()
