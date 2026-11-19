"""HTTP routes for a machine's papers (warranty card, insurance policy, invoice, AMC, manual): server/coverage.py.

GET    /api/docs/{machine}                 papers on file + the coverage verdict for the current fault
POST   /api/docs/{machine}                 upload one paper (multipart: file, kind optional); Claude reads it
GET    /api/docs/{machine}/{doc}/file      the original file
DELETE /api/docs/{machine}/{doc}           remove a paper
GET    /api/coverage/{machine}?lang=hi     the verdict + the WhatsApp words in that language
GET    /api/claim/{machine}?lang=hi        reply "4": claim steps, contact and a claim letter
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from . import coverage, diagnosis


def router(live: Any) -> APIRouter:
    r = APIRouter(prefix="/api")

    def machine(mid: str) -> dict[str, Any]:
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        return live.c.machines[mid]

    def lang_of(mid: str, lang: str | None) -> str:
        return lang if lang in diagnosis.LANGS else live.alerts.owner_lang(mid)

    def verdict(mid: str, lang: str) -> dict[str, Any]:
        card = live.alerts.card(mid, lang)
        cov = (card or {}).get("coverage") or live.alerts.coverage(mid, card)
        return {"coverage": cov, "text": coverage.alert_text(cov, lang), "lang": lang,
                "fault_name": (card or {}).get("name") if card and card.get("fault") else None}  # fmt: skip

    @r.get("/docs/{mid}")
    def list_docs(mid: str, lang: str | None = None) -> dict[str, Any]:
        m = machine(mid)
        live.alerts.docs.seed(mid, m)
        order = {
            k: i for i, k in enumerate(coverage.KINDS)
        }  # warranty, insurance, amc, invoice, manual, other
        docs = [
            coverage.public(d)
            for d in sorted(live.alerts.docs.list(mid), key=lambda d: order.get(d["kind"], 9))
        ]
        return {"machine": mid, "documents": docs, **verdict(mid, lang_of(mid, lang))}

    @r.post("/docs/{mid}")
    async def upload(
        mid: str, file: Annotated[UploadFile, File()], kind: Annotated[str | None, Form()] = None
    ) -> dict[str, Any]:
        m = machine(mid)
        data = await file.read(coverage.MAX_BYTES + 1)
        if not data:
            raise HTTPException(400, "empty file")
        live.alerts.docs.seed(mid, m)  # samples first, so a re-seed never follows an upload
        try:
            doc = await asyncio.to_thread(
                live.alerts.docs.add, mid, m, data, file.filename or "document", kind
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        live.event("document_added", machine=mid, paper=doc["kind"], read_by=doc["read_by"])
        return {"document": coverage.public(doc), **verdict(mid, lang_of(mid, None))}

    @r.get("/docs/{mid}/{doc_id}/file")
    def doc_file(mid: str, doc_id: str) -> Response:
        machine(mid)
        p = live.alerts.docs.file(mid, doc_id)
        if p is None:
            raise HTTPException(404, "no such document")
        return Response(p.read_bytes(), media_type=coverage.MIME.get(p.suffix, "application/octet-stream"))

    @r.delete("/docs/{mid}/{doc_id}")
    def delete_doc(mid: str, doc_id: str) -> dict[str, Any]:
        machine(mid)
        if not live.alerts.docs.delete(mid, doc_id):
            raise HTTPException(404, "no such document")
        live.event("document_removed", machine=mid)
        return {"deleted": doc_id}

    @r.get("/coverage/{mid}")
    def coverage_for(mid: str, lang: str | None = None) -> dict[str, Any]:
        machine(mid)
        return verdict(mid, lang_of(mid, lang))

    @r.get("/claim/{mid}")
    def claim(mid: str, lang: str | None = None) -> dict[str, Any]:
        machine(mid)
        lg = lang_of(mid, lang)
        return {"machine": mid, "lang": lg, "text": live.alerts.claim_text(mid, lg)}

    return r
