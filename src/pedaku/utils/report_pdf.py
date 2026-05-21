"""PDF report writer for a diagnostic session (DTC + live snapshot + ECU info)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import IO, Iterable, Union

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib import colors

from ..core.dtc import Dtc
from ..core.session import EcuInfo


PathOrFile = Union[Path, str, IO[bytes]]


def write_report(
    target: PathOrFile,
    info: EcuInfo,
    dtcs: Iterable[Dtc],
    live_snapshot: dict[str, str],
    health_score: int,
) -> PathOrFile:
    """Write the diagnostic report.

    *target* can be a filesystem path or an in-memory file (e.g.
    :class:`io.BytesIO`). Streaming directly into a buffer is what the
    Flask handler does so it never has to round-trip through ``/tmp`` -
    that avoids a symlink-attack class of bug on shared hosts.
    """
    # SimpleDocTemplate accepts a path or any file-like with .write().
    # We let reportlab do its own type sniffing so the same call works
    # for both Path and BytesIO without callers having to care.
    if isinstance(target, Path):
        doc_target: Union[str, IO[bytes]] = str(target)
    else:
        doc_target = target  # type: ignore[assignment]
    doc = SimpleDocTemplate(doc_target, pagesize=A4, title="Diagnosa Report")
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Laporan Diagnosa Motor", styles["Title"]))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), styles["Normal"]))
    story.append(Spacer(1, 12))

    info_rows = [
        ["VIN", info.vin or "-"],
        ["ECU", info.ecu_name or "-"],
        ["Protocol", info.protocol or "-"],
        ["Adapter", info.adapter or "-"],
        ["Battery (V)", f"{info.voltage:.2f}" if info.voltage else "-"],
        ["Health Score", f"{health_score}/100"],
    ]
    t = Table(info_rows, colWidths=[120, 360])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
    ]))
    story.append(t)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Diagnostic Trouble Codes", styles["Heading2"]))
    dtc_rows = [["Code", "Severity", "Description"]]
    for d in dtcs:
        dtc_rows.append([d.code, d.severity, d.description])
    if len(dtc_rows) == 1:
        dtc_rows.append(["-", "-", "Tidak ada DTC tersimpan."])
    t2 = Table(dtc_rows, colWidths=[60, 70, 350])
    t2.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
    ]))
    story.append(t2)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Live Data Snapshot", styles["Heading2"]))
    live_rows = [["Parameter", "Value"]] + [[k, v] for k, v in live_snapshot.items()]
    t3 = Table(live_rows, colWidths=[240, 240])
    t3.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
    ]))
    story.append(t3)

    doc.build(story)
    return target
