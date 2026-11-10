"""The TMF completeness view of one cut: the expected-against-filed matrix and the
percentages from the cut's eTMF index (quality.tmf_completeness's own assess(), so the page
and the agent agree), linked to the current TMF findings the caller may see.

Repository passes only findings the caller may see; a cell names only those. Nothing here
reads subject data: DM gives the agent each site's first and last subject date only.
"""

from collections.abc import Sequence

from meridian.agents.quality.tmf_completeness import Assessment, Cell
from meridian.api.models import TmfArtifact, TmfCell, TmfFindingItem, TmfSiteRow
from meridian.platform.tmf_reference import TmfReference


def artifacts(ref: TmfReference) -> list[TmfArtifact]:
    return [
        TmfArtifact(
            key=a.key,
            name=a.name,
            zone=a.zone,
            level=a.level,
            milestone=a.milestone,
            file_within_days=a.file_within_days,
            essential=a.essential,
        )
        for a in ref.artifacts
    ]


def _cell(c: Cell, finding_of: dict[str, str]) -> TmfCell:
    return TmfCell(
        artifact=c.artifact,
        status=c.status,
        expected=c.expected,
        filed=c.filed,
        late=c.late,
        finding_ids=sorted({finding_of[r] for r in c.records if r in finding_of}),
    )


def rows(
    a: Assessment, ref: TmfReference, finding_of: dict[str, str]
) -> tuple[list[TmfCell], list[TmfSiteRow]]:
    """Trial cells in artifact order, and one row per site with its site-level cells.
    finding_of: record (the dedup check's record part) to the id of a visible finding."""
    order = {art.key: i for i, art in enumerate(ref.artifacts)}
    trial = sorted((c for c in a.cells if c.site == ""), key=lambda c: order[c.artifact])
    sites = []
    for site in a.sites:
        cells = sorted((c for c in a.cells if c.site == site), key=lambda c: order[c.artifact])
        complete, timely = a.percent(site)
        sites.append(
            TmfSiteRow(
                site_id=site,
                completeness_percent=complete,
                timely_percent=timely,
                cells=[_cell(c, finding_of) for c in cells],
            )
        )
    return [_cell(c, finding_of) for c in trial], sites


def items(found: Sequence[TmfFindingItem]) -> list[TmfFindingItem]:
    severity = {"high": 0, "medium": 1, "low": 2}
    return sorted(found, key=lambda f: (severity[f.severity.value], f.site_id or "", f.finding_id))
