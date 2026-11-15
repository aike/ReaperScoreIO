"""sio_client: 自作の Python プログラムから SIO を使うためのライブラリ（標準ライブラリだけで動く）。

    from sio_client import SIO

    sio = SIO()
    for note in sio.notes(bars=(1, 2)):
        print(note.part, note.pitch, note.bar, note.beat, note.duration)
"""

from .client import PROTOCOL_VERSION, SIO, Patch
from .errors import (
    BridgeUnavailable,
    NoteMismatch,
    NoteNotFound,
    OutcomeUnknown,
    PartNotFound,
    PartNotSelected,
    ProjectMismatch,
    RequestPending,
    SIOConfigError,
    SIOConnectionError,
    SIOError,
    SIOException,
)
from .types import (
    Diagnostic,
    Inserted,
    Meter,
    Note,
    NoteList,
    Part,
    PatchResult,
    Project,
    ScoreInfo,
    Selection,
    Status,
    Track,
    pitch_name,
    pitch_number,
)

CLIENT_VERSION = "0.1.0"

__all__ = [
    "SIO", "Patch", "PROTOCOL_VERSION", "CLIENT_VERSION",
    "Note", "NoteList", "Part", "Project", "Meter", "Diagnostic", "ScoreInfo", "Selection", "Status", "Track",
    "PatchResult", "Inserted", "pitch_name", "pitch_number",
    "SIOException", "SIOConfigError", "SIOConnectionError", "SIOError", "BridgeUnavailable", "RequestPending",
    "OutcomeUnknown", "ProjectMismatch", "NoteMismatch", "NoteNotFound", "PartNotFound", "PartNotSelected",
]
