"""HTTP API: the shared cluster registry and the folk-label endpoint.

    uvicorn server.registry_api:create_app --factory --port 8100

POST /fingerprints   publish a confirmed fault (embedding + type + owner's label)
GET  /fingerprints   list, optionally ?machine_type=pump
POST /match          cosine-match one reading against the registry
POST /labels/text    owner's text note -> weak label (stored)
POST /labels/voice   owner's voice note -> Whisper -> weak label (audio discarded)
"""

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from edge.config import EMBED_DIM, MACHINE_TYPES

from . import folk, provider, registry
from .config import REGISTRY_DB


class Fingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")  # anything else (e.g. audio) is rejected
    machine_type: str
    fault: str
    owner_label: str = Field(max_length=200)
    factory: str
    machine: str
    embedding: list[float] = Field(min_length=EMBED_DIM, max_length=EMBED_DIM)
    weak_label_id: int | None = None
    idem: str | None = Field(default=None, max_length=64)  # outbox idempotency key


class MatchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_type: str
    embedding: list[float] = Field(min_length=EMBED_DIM, max_length=EMBED_DIM)
    exclude_machine: str | None = None


class TextNote(BaseModel):
    text: str = Field(max_length=2000)
    language: str | None = None
    factory: str = ""
    machine: str = ""


def create_app(db_path: Path = REGISTRY_DB, llm: folk.LLM | None = None) -> FastAPI:
    """`llm` overrides the Granite fallback (tests pass a stub or None)."""
    app = FastAPI(title="Dhadkan registry")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = registry.connect(db_path)
    use_llm = llm if llm is not None else (folk.llm_label if provider.available() else None)

    def check_type(t: str) -> None:
        if t not in MACHINE_TYPES:
            raise HTTPException(422, f"machine_type must be one of {MACHINE_TYPES}")

    @app.post("/fingerprints")
    def publish(fp: Fingerprint) -> dict[str, Any]:
        check_type(fp.machine_type)
        try:
            fid = registry.publish(
                db,
                fp.machine_type,
                fp.fault,
                fp.owner_label,
                fp.factory,
                fp.machine,
                fp.embedding,
                fp.weak_label_id,
                fp.idem,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": fid}

    @app.get("/fingerprints")
    def list_fps(machine_type: str | None = None) -> list[dict[str, Any]]:
        return registry.fingerprints(db, machine_type)

    @app.post("/match")
    def match(q: MatchQuery) -> list[dict[str, Any]]:
        check_type(q.machine_type)
        try:
            return registry.match(db, q.machine_type, q.embedding, q.exclude_machine)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    def _label(text: str, language: str | None, factory: str, machine: str) -> dict[str, Any]:
        label = folk.label_text(text, folk.load_lexicon(), use_llm)
        label["id"] = registry.add_weak_label(db, factory, machine, text, language, label)
        label["text"] = text
        return label

    @app.post("/labels/text")
    def label_text(note: TextNote) -> dict[str, Any]:
        return _label(note.text, note.language, note.factory, note.machine)

    @app.post("/labels/voice")
    def label_voice(
        audio: Annotated[UploadFile, File()],
        factory: Annotated[str, Form()] = "",
        machine: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(suffix=Path(audio.filename or "n.wav").suffix) as tmp:
            tmp.write(audio.file.read())
            tmp.flush()
            text, lang = folk.transcribe(tmp.name)
        return _label(text, lang, factory, machine)

    app.state.db = db
    return app
