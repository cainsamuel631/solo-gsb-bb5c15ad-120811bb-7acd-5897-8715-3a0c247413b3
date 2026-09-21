"""法规条款变更审阅 Web 应用 —— FastAPI 入口。

启动时初始化 SQLite 数据库，页面与接口均通过 8000 端口访问。
"""
from __future__ import annotations

import csv
import io
import json
import os
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alignment as al
from . import database as db

app = FastAPI(title="法规条款变更审阅", version="1.0.0")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@app.on_event("startup")
def on_startup() -> None:
    # 容器启动时初始化数据库（建表幂等）
    db.init_db()


# ---------------------------------------------------------------- 数据模型

class AlignmentPatch(BaseModel):
    old_idx: Optional[list[int]] = Field(default=None)
    new_idx: Optional[list[int]] = Field(default=None)
    change_type: Optional[str] = None
    decision: Optional[str] = None
    decision_note: Optional[str] = None
    status: Optional[str] = None


class RenameBody(BaseModel):
    name: str


class RealignBody(BaseModel):
    old_text: Optional[str] = None
    new_text: Optional[str] = None


# ---------------------------------------------------------------- 辅助函数

def _require_batch(conn, batch_id: int):
    batch = db.get_batch(conn, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


def _paragraph_lists(conn, batch_id: int) -> tuple[list[str], list[str]]:
    old = [r["text"] for r in db.get_paragraphs(conn, batch_id, "old")]
    new = [r["text"] for r in db.get_paragraphs(conn, batch_id, "new")]
    return old, new


def _validate_indices(indices: list[int], size: int, side: str) -> None:
    if any(i < 1 or i > size for i in indices):
        raise HTTPException(
            status_code=400,
            detail=f"{side}段落序号超出范围（1~{size}）: {indices}",
        )
    if len(set(indices)) != len(indices):
        raise HTTPException(status_code=400, detail=f"{side}段落序号存在重复")


def _alignment_dict(row, old_texts, new_texts) -> dict[str, Any]:
    old_idx = db.parse_idx(row["old_idx"])
    new_idx = db.parse_idx(row["new_idx"])
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "old_idx": old_idx,
        "new_idx": new_idx,
        "change_type": row["change_type"],
        "change_label": al.TYPE_LABELS[row["change_type"]],
        "confidence": row["confidence"],
        "rationale": json.loads(row["rationale"]),
        "is_manual": bool(row["is_manual"]),
        "status": row["status"],
        "decision": row["decision"],
        "decision_note": row["decision_note"],
        "decided_at": row["decided_at"],
        "old_paragraphs": [
            {"idx": i, "text": old_texts[i - 1]} for i in old_idx
        ],
        "new_paragraphs": [
            {"idx": i, "text": new_texts[i - 1]} for i in new_idx
        ],
    }


def _read_upload(f: UploadFile) -> str:
    raw = f.file.read()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 批次接口

@app.get("/api/batches")
def api_list_batches():
    with db.db_conn() as conn:
        return [dict(r) for r in db.list_batches(conn)]


@app.post("/api/batches")
async def api_create_batch(
    name: str = Form(...),
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
):
    old_text = _read_upload(old_file)
    new_text = _read_upload(new_file)
    old_paras = al.split_paragraphs(old_text)
    new_paras = al.split_paragraphs(new_text)
    if not old_paras and not new_paras:
        raise HTTPException(status_code=400, detail="两份文本均为空，无法建立对齐")

    rows = al.align(old_paras, new_paras)
    with db.db_conn() as conn:
        batch_id = db.create_batch(
            conn, name.strip() or "未命名批次",
            old_file.filename, new_file.filename,
            old_paras, new_paras, rows,
        )
    return {"id": batch_id, "alignment_count": len(rows)}


@app.get("/api/batches/{batch_id}")
def api_get_batch(batch_id: int):
    with db.db_conn() as conn:
        _require_batch(conn, batch_id)
        old_texts, new_texts = _paragraph_lists(conn, batch_id)
        alignments = [
            _alignment_dict(r, old_texts, new_texts)
            for r in db.list_alignments(conn, batch_id)
        ]
        batch = dict(db.get_batch(conn, batch_id))
    return {
        "batch": batch,
        "old_paragraphs": [
            {"idx": i + 1, "text": t} for i, t in enumerate(old_texts)
        ],
        "new_paragraphs": [
            {"idx": i + 1, "text": t} for i, t in enumerate(new_texts)
        ],
        "alignments": alignments,
    }


@app.delete("/api/batches/{batch_id}")
def api_delete_batch(batch_id: int):
    with db.db_conn() as conn:
        _require_batch(conn, batch_id)
        db.delete_batch(conn, batch_id)
    return {"ok": True}


@app.post("/api/batches/{batch_id}/realign")
async def api_realign(batch_id: int, body: RealignBody):
    """用（可选修改后的）两版文本重新生成对齐候选，原有审阅记录清空。"""
    with db.db_conn() as conn:
        batch_row = _require_batch(conn, batch_id)
        old_texts, new_texts = _paragraph_lists(conn, batch_id)

        old_paras = (
            al.split_paragraphs(body.old_text)
            if body.old_text is not None else old_texts
        )
        new_paras = (
            al.split_paragraphs(body.new_text)
            if body.new_text is not None else new_texts
        )
        if not old_paras and not new_paras:
            raise HTTPException(status_code=400, detail="两份文本均为空")

        rows = al.align(old_paras, new_paras)
        # 保留原批次名称与文件名，重建段落与候选
        name, of, nf = (
            batch_row["name"], batch_row["old_filename"],
            batch_row["new_filename"],
        )
        db.delete_batch(conn, batch_id)
        new_id = db.create_batch(
            conn, name, of, nf, old_paras, new_paras, rows,
        )
    return {"id": new_id, "alignment_count": len(rows)}


# ---------------------------------------------------------------- 对齐接口

@app.patch("/api/alignments/{alignment_id}")
def api_update_alignment(alignment_id: int, patch: AlignmentPatch):
    with db.db_conn() as conn:
        row = db.get_alignment(conn, alignment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="对齐候选不存在")
        batch_id = row["batch_id"]
        old_texts, new_texts = _paragraph_lists(conn, batch_id)

        fields: dict[str, Any] = {}
        mapping_changed = (
            patch.old_idx is not None or patch.new_idx is not None
        )

        if mapping_changed:
            old_idx = (
                patch.old_idx if patch.old_idx is not None
                else db.parse_idx(row["old_idx"])
            )
            new_idx = (
                patch.new_idx if patch.new_idx is not None
                else db.parse_idx(row["new_idx"])
            )
            _validate_indices(old_idx, len(old_texts), "旧版")
            _validate_indices(new_idx, len(new_texts), "新版")
            if not old_idx and not new_idx:
                raise HTTPException(
                    status_code=400,
                    detail="旧版与新版映射不能同时为空（至少选择一侧段落）",
                )
            # 收回被其他候选占用的段落（审阅者手动调整映射即视为重新分配），
            # 并重算受影响候选的变更类型/置信度/依据
            affected = db.reclaim_paragraphs(
                conn, batch_id, old_idx, new_idx, alignment_id
            )
            for aid in affected:
                if aid < 0:
                    continue  # 段落全部被收回的候选已删除
                arow = db.get_alignment(conn, aid)
                ao = db.parse_idx(arow["old_idx"])
                an = db.parse_idx(arow["new_idx"])
                ares = al.reclassify_manual(old_texts, new_texts, ao, an)
                db.update_alignment(
                    conn, aid,
                    {
                        "old_idx": db._pack_idx(ares["old_idx"]),
                        "new_idx": db._pack_idx(ares["new_idx"]),
                        "change_type": ares["change_type"],
                        "confidence": ares["confidence"],
                        "rationale": json.dumps(
                            ares["rationale"], ensure_ascii=False
                        ),
                    },
                )
            result = al.reclassify_manual(
                old_texts, new_texts, old_idx, new_idx,
                explicit_type=patch.change_type,
            )
            fields.update(
                old_idx=db._pack_idx(result["old_idx"]),
                new_idx=db._pack_idx(result["new_idx"]),
                change_type=result["change_type"],
                confidence=result["confidence"],
                rationale=json.dumps(
                    result["rationale"], ensure_ascii=False
                ),
                is_manual=1,
            )
        elif patch.change_type is not None:
            # 只改类型：保留原映射，重新生成依据
            if patch.change_type not in db.VALID_TYPES:
                raise HTTPException(status_code=400, detail="非法变更类型")
            old_idx = db.parse_idx(row["old_idx"])
            new_idx = db.parse_idx(row["new_idx"])
            result = al.reclassify_manual(
                old_texts, new_texts, old_idx, new_idx,
                explicit_type=patch.change_type,
            )
            fields.update(
                change_type=result["change_type"],
                confidence=result["confidence"],
                rationale=json.dumps(
                    result["rationale"], ensure_ascii=False
                ),
                is_manual=1,
            )

        if patch.decision is not None:
            decision = patch.decision.strip() or None
            if decision and decision not in ("approve", "reject", "modify"):
                raise HTTPException(
                    status_code=400,
                    detail="decision 只能为 approve / reject / modify",
                )
            fields["decision"] = decision
            from datetime import datetime, timezone
            fields["decided_at"] = (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
                if decision else None
            )
            # 记录了审阅决定即视为已确认；清除决定回到待审阅
            fields["status"] = "confirmed" if decision else "pending"

        if patch.decision_note is not None:
            note = patch.decision_note.strip()
            fields["decision_note"] = note or None

        if patch.status is not None:
            if patch.status not in db.VALID_STATUSES:
                raise HTTPException(status_code=400, detail="非法状态")
            fields["status"] = patch.status

        if fields:
            db.update_alignment(conn, alignment_id, fields)

        row = db.get_alignment(conn, alignment_id)
        return _alignment_dict(row, old_texts, new_texts)


# ---------------------------------------------------------------- 导出

EXPORT_HEADERS = [
    "对齐编号", "变更类型", "变更类型(英文)", "置信度", "审阅状态",
    "审阅决定", "审阅备注", "是否手动调整",
    "旧版段落序号", "新版段落序号", "旧版原文", "新版原文",
]

_DECISION_LABELS = {"approve": "认可", "reject": "驳回", "modify": "需修改"}
_STATUS_LABELS = {"pending": "待审阅", "confirmed": "已确认"}


def _export_rows(conn, batch_id: int) -> list[dict[str, str]]:
    old_texts, new_texts = _paragraph_lists(conn, batch_id)
    out = []
    for r in db.list_alignments(conn, batch_id):
        old_idx = db.parse_idx(r["old_idx"])
        new_idx = db.parse_idx(r["new_idx"])
        out.append(
            {
                "对齐编号": r["id"],
                "变更类型": al.TYPE_LABELS[r["change_type"]],
                "变更类型(英文)": r["change_type"],
                "置信度": f"{r['confidence']:.3f}",
                "审阅状态": _STATUS_LABELS[r["status"]],
                "审阅决定": _DECISION_LABELS.get(r["decision"] or "", ""),
                "审阅备注": r["decision_note"] or "",
                "是否手动调整": "是" if r["is_manual"] else "否",
                "旧版段落序号": ",".join(map(str, old_idx)),
                "新版段落序号": ",".join(map(str, new_idx)),
                "旧版原文": "\n---\n".join(old_texts[i - 1] for i in old_idx),
                "新版原文": "\n---\n".join(new_texts[i - 1] for i in new_idx),
            }
        )
    return out


@app.get("/api/batches/{batch_id}/export.csv")
def api_export_csv(batch_id: int):
    with db.db_conn() as conn:
        batch = _require_batch(conn, batch_id)
        rows = _export_rows(conn, batch_id)

    buf = io.StringIO()
    buf.write("﻿")  # BOM，方便 Excel 识别 UTF-8
    writer = csv.DictWriter(buf, fieldnames=EXPORT_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)

    filename = f"batch-{batch_id}-review.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )


@app.get("/api/batches/{batch_id}/export.json")
def api_export_json(batch_id: int):
    with db.db_conn() as conn:
        batch = _require_batch(conn, batch_id)
        old_texts, new_texts = _paragraph_lists(conn, batch_id)
        alignments = [
            _alignment_dict(r, old_texts, new_texts)
            for r in db.list_alignments(conn, batch_id)
        ]
    payload = {
        "batch": dict(batch),
        "decision_legend": {
            "approve": "认可", "reject": "驳回", "modify": "需修改",
            "pending": "待审阅", "confirmed": "已确认",
        },
        "alignments": alignments,
    }
    filename = f"batch-{batch_id}-review.json"
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )


# ---------------------------------------------------------------- 静态页面

@app.get("/healthz")
def healthz():
    return {"ok": True}


if os.path.isdir(STATIC_DIR):
    app.mount(
        "/", StaticFiles(directory=STATIC_DIR, html=True), name="static"
    )
