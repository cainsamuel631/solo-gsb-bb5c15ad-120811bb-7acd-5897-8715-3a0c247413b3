"""条款对齐算法。

处理管线
--------
1. :func:`split_paragraphs` 把纯文本切分为有序段落；
2. difflib.SequenceMatcher 按段落顺序生成 equal / replace / delete / insert 块；
3. equal 块为“无变更”；delete/insert 块在对面版本的邻近窗口寻找残留相似证据，
   判为删除/新增；
4. replace 块内部先按相似度贪心挑选**非交叉锚点**（1:1 骨架，保持段落顺序），
   再处理锚点之间的空隙：空隙内配对（多对多重写）、未配对段落就近挂接到
   锚点段（1→N 拆分、N→1 合并），并用链式闭合支持三段以上的拆分/合并；
5. 每条候选附带 confidence 与可解释 rationale（判定依据 + 两两相似度证据）。

审阅者手动调整映射时调用 :func:`reclassify_manual` 重新判定类型与依据。

注意：成边判定使用 :func:`_attach_signal`（相似度 + 包含率 + 条号结构证据），
但边中始终保存**原始相似度**，保证置信度与证据不被结构奖励放大。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Optional

# ---- 类型常量 -------------------------------------------------------------
UNCHANGED = "unchanged"
ADDED = "added"
DELETED = "deleted"
SPLIT = "split"
MERGED = "merged"
REWRITTEN = "rewritten"

# ---- 判定阈值 -------------------------------------------------------------
ANCHOR_THRESHOLD = 0.45    # 非交叉锚点（1:1 骨架，改写/对应关系）的最低相似度
PAIR_THRESHOLD = 0.35      # 空隙内残余 N:M 配对成边的最低相似度
SPLIT_ATTACH = 0.50        # 未配对新段挂到旧段（拆分）的信号门槛
MERGE_ATTACH = 0.45        # 未配对旧段挂到新段（合并）的信号门槛
CHAIN_ATTACH = 0.40        # 链式挂载（相邻段已映射同一目标，纯文本信号）门槛
MATCH_THRESHOLD = 0.92     # 1:1 相似度 >= 该值视为无变更
SPLIT_MEAN_THRESHOLD = 0.30  # 1:N 平均相似度低于该值则退化为新增+删除
EVIDENCE_WINDOW = 4        # delete/insert 块在对面版本搜索证据的邻近窗口
ATTACH_DISTANCE = 2        # 空隙内挂载允许的最大块内位置距离

TYPE_LABELS = {
    UNCHANGED: "无变更",
    ADDED: "新增",
    DELETED: "删除",
    SPLIT: "拆分",
    MERGED: "合并",
    REWRITTEN: "改写",
}

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9a-z]+|[一-鿿]")
_BIGRAM_RE = re.compile(r"[一-鿿]")

# 段落首部条号：第四条 / 第4条 / 第12条之一 / 第4-2条 / 第4.2条
_CLAUSE_NUM_RE = re.compile(
    r"^\s*第\s*([0-9]+|[一二三四五六七八九十百千零两]+)\s*条"
    r"(?:\s*[之\-．.·]\s*([0-9]+|[一二三四五六七八九十]+))?"
)

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    total = 0
    if "百" in s:
        a, _, rest = s.partition("百")
        total = _CN_NUM.get(a, 1) * 100
        s = rest
        if not s:
            return total
    if "十" in s:
        a, _, b = s.partition("十")
        total += _CN_NUM.get(a, 1) * 10
        total += _CN_NUM.get(b, 0) if b else 0
        return total
    return total or None


def clause_number(text: str) -> Optional[tuple[int, Optional[int]]]:
    """提取段首条号 (主号, 子号)；如「第四条之一」-> (4, 1)，无条号返回 None。"""
    m = _CLAUSE_NUM_RE.match(text)
    if not m:
        return None
    main = _cn_to_int(m.group(1))
    sub = _cn_to_int(m.group(2)) if m.group(2) else None
    if main is None:
        return None
    return main, sub


# ---------------------------------------------------------------- 段落切分

def split_paragraphs(text: str) -> list[str]:
    """把纯文本切分为有序段落。

    优先按空行分段；若全文没有空行，则按行切分，并将明显属于同一段
    自动折行的相邻行拼接（行尾与下行行首均为中日韩字符时直接连接，
    否则以空格连接）。
    """
    if not text or not text.strip():
        return []

    chunks = re.split(r"\n\s*\n", text.strip())
    if len(chunks) > 1:
        return _non_empty(chunks)

    # 无空行：按行切分并尝试合并折行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    paragraphs: list[str] = []
    for line in lines:
        if paragraphs and _looks_wrapped(paragraphs[-1], line):
            joiner = "" if _both_cjk(paragraphs[-1], line) else " "
            paragraphs[-1] = paragraphs[-1] + joiner + line
        else:
            paragraphs.append(line)
    return paragraphs


def _non_empty(chunks: list[str]) -> list[str]:
    out = []
    for c in chunks:
        c = c.strip()
        if c:
            # 段落内部统一折叠空白，保留可读性
            out.append(_WS_RE.sub(" ", c).strip())
    return out


def _looks_wrapped(prev: str, nxt: str) -> bool:
    if not prev or not nxt:
        return False
    # 上一行以句末标点结尾，或下一行像新条目（条/第/数字编号开头），不合并
    end_punct = "。；！？;!?.．…）)】》”’"
    if prev.rstrip()[-1] in end_punct:
        return False
    if re.match(
        r"^(第[一二三四五六七八九十百千0-9]+[条章节]|[0-9]+[.、)）]|[（(][0-9]+[)）])",
        nxt,
    ):
        return False
    return _CJK_RE.search(prev[-1] + nxt[0]) is not None


def _both_cjk(prev: str, nxt: str) -> bool:
    return bool(_CJK_RE.search(prev[-1]) and _CJK_RE.search(nxt[0]))


# ---------------------------------------------------------------- 相似度

def _normalize(text: str) -> str:
    return _WS_RE.sub("", text.lower())


def _tokens(text: str) -> set[str]:
    """中英混合 token：拉丁/数字单词 + 中文单字与相邻双字。"""
    flat = _normalize(text)
    tokens: set[str] = set(_WORD_RE.findall(flat))
    cjk = _BIGRAM_RE.findall(flat)
    tokens.update(cjk)
    tokens.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def similarity(a: str, b: str) -> float:
    """混合相似度：词/字 Jaccard 与字符序列相似度各占一半，兼顾用词与顺序。"""
    if not a or not b:
        return 0.0
    ta, tb = _tokens(a), _tokens(b)
    union = ta | tb
    jaccard = len(ta & tb) / len(union) if union else 0.0
    seq = SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()
    return round(0.5 * jaccard + 0.5 * seq, 4)


def _similarity_matrix(a: list[str], b: list[str]) -> list[list[float]]:
    return [[similarity(x, y) for y in b] for x in a]


# ---------------------------------------------------------------- 主入口

def align(
    old_paragraphs: list[str], new_paragraphs: list[str]
) -> list[dict[str, Any]]:
    """生成按文档顺序排列的对齐候选行。

    每行::

        {"old_idx": [..], "new_idx": [..], "change_type": str,
         "confidence": float, "rationale": {...}}
    序号为 1 基。
    """
    rows: list[dict[str, Any]] = []
    sm = SequenceMatcher(None, old_paragraphs, new_paragraphs, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_block = old_paragraphs[i1:i2]
        new_block = new_paragraphs[j1:j2]

        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(_unchanged_row(i1 + k, j1 + k))
        elif tag == "delete":
            rows.extend(_deleted_rows(old_block, new_paragraphs, i1))
        elif tag == "insert":
            rows.extend(_added_rows(new_block, old_paragraphs, j1))
        else:  # replace
            rows.extend(_refine_replace(old_block, new_block, i1, j1))

    return _order_rows(rows, len(old_paragraphs), len(new_paragraphs))


# ---------------------------------------------------------------- 行构造

def _unchanged_row(oi: int, ni: int) -> dict[str, Any]:
    return {
        "old_idx": [oi + 1],
        "new_idx": [ni + 1],
        "change_type": UNCHANGED,
        "confidence": 1.0,
        "rationale": {
            "summary": "旧版与新版段落完全一致，按段落顺序自动匹配。",
            "evidence": {"rule": "sequence_equal"},
        },
    }


def _best_evidence(
    block: list[str], other: list[str], at: int
) -> tuple[float, int]:
    """在 other 的邻近窗口内找与 block 最相似的段落，返回 (最高相似度, 1 基序号)。"""
    if not other:
        return 0.0, 0
    lo = max(0, at - EVIDENCE_WINDOW)
    hi = min(len(other), at + len(block) - 1 + EVIDENCE_WINDOW + 1)
    best, best_pos = 0.0, -1
    for p in range(lo, hi):
        s = max(similarity(t, other[p]) for t in block)
        if s > best:
            best, best_pos = s, p
    return round(best, 3), best_pos + 1


def _deleted_rows(
    block: list[str], new_texts: list[str], start: int
) -> list[dict[str, Any]]:
    rows = []
    for k, text in enumerate(block):
        best, pos = _best_evidence([text], new_texts, start + k)
        rationale: dict[str, Any] = {
            "summary": (
                f"该旧版段落顺序对齐到删除区域，新版中无对应段落，判定为删除"
                f"（依据：新版邻近段落最高相似度 {best:.2f}，相似度越低越可信）。"
            ),
            "evidence": {"rule": "deleted_no_match", "best_new_similarity": best},
        }
        if pos:
            rationale["evidence"]["nearest_new_paragraph"] = pos
        rows.append(
            {
                "old_idx": [start + k + 1],
                "new_idx": [],
                "change_type": DELETED,
                "confidence": _confidence(best, DELETED),
                "rationale": rationale,
            }
        )
    return rows


def _added_rows(
    block: list[str], old_texts: list[str], start: int
) -> list[dict[str, Any]]:
    rows = []
    for k, text in enumerate(block):
        best, pos = _best_evidence([text], old_texts, start + k)
        rationale: dict[str, Any] = {
            "summary": (
                f"该新版段落顺序对齐到插入区域，旧版中无对应段落，判定为新增"
                f"（依据：旧版邻近段落最高相似度 {best:.2f}，相似度越低越可信）。"
            ),
            "evidence": {"rule": "added_no_match", "best_old_similarity": best},
        }
        if pos:
            rationale["evidence"]["nearest_old_paragraph"] = pos
        rows.append(
            {
                "old_idx": [],
                "new_idx": [start + k + 1],
                "change_type": ADDED,
                "confidence": _confidence(best, ADDED),
                "rationale": rationale,
            }
        )
    return rows


# ---------------------------------------------------------------- replace 块

def _block_edges(
    old_block: list[str], new_block: list[str]
) -> tuple[list[tuple[int, int, float]], list[list[float]]]:
    """replace 块内成边（块内 0 基序号），遵循“段落顺序不变”假设。

    1. 相似度矩阵上按分数降序贪心挑选**非交叉锚点**（阈值 ANCHOR_THRESHOLD），
       锚点两侧序号必须单调递增，杜绝远距离共享词汇造成的错配；
    2. 相邻锚点之间的空隙（含块首/块尾）交给 :func:`_gap_edges` 处理。
    """
    n_o, n_n = len(old_block), len(new_block)
    mat = _similarity_matrix(old_block, new_block)

    candidates = sorted(
        (
            (mat[oi][ni], abs(oi - ni), oi, ni)
            for oi in range(n_o)
            for ni in range(n_n)
        ),
        key=lambda x: (-x[0], x[1]),
    )
    anchors: list[tuple[int, int, float]] = []
    used_o: set[int] = set()
    used_n: set[int] = set()
    for sim, _dist, oi, ni in candidates:
        if sim < ANCHOR_THRESHOLD:
            break
        if oi in used_o or ni in used_n:
            continue
        if anchors and not _order_ok(anchors, oi, ni):
            continue
        anchors.append((oi, ni, round(sim, 3)))
        used_o.add(oi)
        used_n.add(ni)

    edges = list(anchors)

    bounds = [(-1, -1)] + [
        (ao, an) for ao, an, _ in sorted(anchors)
    ] + [(n_o, n_n)]
    for k in range(len(bounds) - 1):
        o0, n0 = bounds[k]
        o1, n1 = bounds[k + 1]
        gap_o = list(range(o0 + 1, o1))
        gap_n = list(range(n0 + 1, n1))
        prev_a = None if k == 0 else bounds[k]
        next_a = None if k == len(bounds) - 2 else bounds[k + 1]
        edges.extend(
            _gap_edges(
                gap_o, gap_n, mat, prev_a, next_a, old_block, new_block
            )
        )

    # 3. 全块闭合：把块首/块尾未配对段落沿相邻关系挂到已有分量
    edges = _block_closure(edges, n_o, n_n, old_block, new_block, mat)

    return edges, mat


def _block_closure(
    edges: list[tuple[int, int, float]],
    n_o: int,
    n_n: int,
    old_block: list[str],
    new_block: list[str],
    mat: list[list[float]],
) -> list[tuple[int, int, float]]:
    """全块链式闭合，支持块首/块尾的多段合并与拆分。

    迭代地把未配对段落挂到相邻已配对段落的目标上（纯文本信号，无条号
    奖励，阈值 CHAIN_ATTACH）。为避免跨分量串联，候选段的两个相邻位置
    都只能映射到**同一个目标段**，即该目标被其邻居一致指向。
    例如三段合并：仅末段成为锚点时，中段经相邻链挂上、首段再经中段挂上。
    """
    edges = list(edges)
    paired_o = {lo for lo, _, _ in edges}
    paired_n = {ln for _, ln, _ in edges}

    def neighbor_targets(
        pos: int, targets: dict[int, set[int]]
    ) -> Optional[set[int]]:
        """取 pos 相邻位置的映射目标；仅当邻居一致指向同一目标时返回之。"""
        cand: Optional[set[int]] = None
        for adj in (pos - 1, pos + 1):
            t = targets.get(adj)
            if t:
                if cand is None:
                    cand = set(t)
                else:
                    cand &= t
        return cand

    changed = True
    while changed:
        changed = False

        new_targets: dict[int, set[int]] = {}
        for lo, ln, _s in edges:
            new_targets.setdefault(ln, set()).add(lo)
        for ln in range(n_n):
            if ln in paired_n:
                continue
            cand = neighbor_targets(ln, new_targets)
            if not cand:
                continue
            best = None
            for lo in cand:
                sig = _attach_signal(
                    new_block[ln], old_block[lo],
                    split=True, adjacent=False,
                )
                if sig >= CHAIN_ATTACH and (best is None or sig > best[0]):
                    best = (sig, lo)
            if best:
                edges.append((best[1], ln, round(mat[best[1]][ln], 3)))
                paired_n.add(ln)
                changed = True

        old_targets: dict[int, set[int]] = {}
        for lo, ln, _s in edges:
            old_targets.setdefault(lo, set()).add(ln)
        for lo in range(n_o):
            if lo in paired_o:
                continue
            cand = neighbor_targets(lo, old_targets)
            if not cand:
                continue
            best = None
            for ln in cand:
                sig = _attach_signal(
                    old_block[lo], new_block[ln],
                    split=False, adjacent=False,
                )
                if sig >= CHAIN_ATTACH and (best is None or sig > best[0]):
                    best = (sig, ln)
            if best:
                edges.append((lo, best[1], round(mat[lo][best[1]], 3)))
                paired_o.add(lo)
                changed = True

    return edges


def _order_ok(anchors, oi: int, ni: int) -> bool:
    """(oi,ni) 与已选锚点保持两侧序号同时递增（不交叉）。"""
    less = [a for a in anchors if a[0] < oi]
    greater = [a for a in anchors if a[0] > oi]
    if less and ni <= max(a[1] for a in less):
        return False
    if greater and ni >= min(a[1] for a in greater):
        return False
    return True


def _attach_signal(
    fragment: str,
    source: str,
    split: bool = True,
    adjacent: bool = False,
) -> float:
    """碎片挂接到长段的信号：综合相似度、token 包含率与条号结构证据。

    - 相似度：整体文本相似程度；
    - 包含率：碎片 token 被源段包含的比例（短碎片改写后 Jaccard 偏低）；
    - 条号证据：碎片为「第四条之一/第4-2条」等子条、源段为同主号「第四条」
      且两者在文档中紧邻（adjacent）时，是法规拆分/合并的强结构信号。
    """
    sim = similarity(fragment, source)
    ft, st = _tokens(fragment), _tokens(source)
    contain = len(ft & st) / len(ft) if ft else 0.0
    text_score = max(sim, contain)
    score = text_score

    fn, sn = clause_number(fragment), clause_number(source)
    if fn and sn and fn[0] == sn[0] and adjacent:
        is_sub_to_main = split and fn[1] is not None and sn[1] is None
        is_main_to_sub = (not split) and sn[1] is not None and fn[1] is None
        if is_sub_to_main or is_main_to_sub:
            score = max(score, min(0.85, 0.55 + 0.5 * text_score))
    return round(score, 3)


def _gap_edges(
    gap_o: list[int],
    gap_n: list[int],
    mat: list[list[float]],
    prev_a,
    next_a,
    old_block: list[str],
    new_block: list[str],
) -> list[tuple[int, int, float]]:
    """处理两个相邻锚点之间（或块首/块尾）的未配对段落。

    1. 两侧都有：空隙内部按相似度降序非交叉贪心配对（多对多重写）；
    2. 未配对新段挂到旧段（空隙内或相邻锚点旧段），形成 1→N 拆分；
    3. 未配对旧段挂到新段（空隙内或相邻锚点新段），形成 N→1 合并；
    4. 链式闭合沿相邻段落扩展多段合并/拆分。

    成边判定使用 :func:`_attach_signal`，但边中保存**原始相似度**。
    """
    edges: list[tuple[int, int, float]] = []
    if not gap_o and not gap_n:
        return edges

    paired_o: set[int] = set()
    paired_n: set[int] = set()

    def add_edge(lo: int, ln: int) -> None:
        edges.append((lo, ln, round(mat[lo][ln], 3)))
        paired_o.add(lo)
        paired_n.add(ln)

    # 1. 空隙内部非交叉贪心配对
    if gap_o and gap_n:
        for sim, lo, ln in sorted(
            ((mat[lo][ln], lo, ln) for lo in gap_o for ln in gap_n),
            reverse=True,
        ):
            if sim < PAIR_THRESHOLD:
                break
            if lo in paired_o or ln in paired_n:
                continue
            add_edge(lo, ln)

    prev_old = prev_a[0] if prev_a is not None else None
    prev_newp = prev_a[1] if prev_a is not None else None
    next_old = next_a[0] if next_a is not None else None
    next_newp = next_a[1] if next_a is not None else None

    # 2. 未配对新段 -> 挂到旧段（1→N 拆分）
    for ln in gap_n:
        if ln in paired_n:
            continue
        choices: list[tuple[float, int]] = [
            (
                _attach_signal(
                    new_block[ln], old_block[lo], split=True,
                    adjacent=abs(lo - ln) == 1,
                ),
                lo,
            )
            for lo in gap_o
            if abs(lo - ln) <= ATTACH_DISTANCE
        ]
        # 前锚点旧段：拆分尾段紧跟锚点新段之后
        if (
            prev_old is not None
            and prev_newp is not None
            and ln == prev_newp + 1
        ):
            choices.append(
                (
                    _attach_signal(
                        new_block[ln], old_block[prev_old],
                        split=True, adjacent=True,
                    ),
                    prev_old,
                )
            )
        # 后锚点旧段：拆出头部紧贴后锚点新段之前
        if (
            next_old is not None
            and next_newp is not None
            and ln + 1 == next_newp
        ):
            choices.append(
                (
                    _attach_signal(
                        new_block[ln], old_block[next_old],
                        split=True, adjacent=True,
                    ),
                    next_old,
                )
            )
        if choices:
            sig, lo = max(choices, key=lambda c: c[0])
            if sig >= SPLIT_ATTACH:
                add_edge(lo, ln)

    # 3. 未配对旧段 -> 挂到新段（N→1 合并）
    for lo in gap_o:
        if lo in paired_o:
            continue
        choices = [
            (
                _attach_signal(
                    old_block[lo], new_block[ln], split=False,
                    adjacent=abs(lo - ln) == 1,
                ),
                ln,
            )
            for ln in gap_n
            if abs(lo - ln) <= ATTACH_DISTANCE
        ]
        # 后锚点新段：合并头段紧贴锚点旧段之前
        if (
            next_newp is not None
            and next_old is not None
            and lo + 1 == next_old
        ):
            choices.append(
                (
                    _attach_signal(
                        old_block[lo], new_block[next_newp],
                        split=False, adjacent=True,
                    ),
                    next_newp,
                )
            )
        # 前锚点新段：合并尾部紧跟前锚点旧段之后
        if (
            prev_newp is not None
            and prev_old is not None
            and lo == prev_old + 1
        ):
            choices.append(
                (
                    _attach_signal(
                        old_block[lo], new_block[prev_newp],
                        split=False, adjacent=True,
                    ),
                    prev_newp,
                )
            )
        if choices:
            sig, ln = max(choices, key=lambda c: c[0])
            if sig >= MERGE_ATTACH:
                add_edge(lo, ln)

    # 4. 链式闭合：未配对段落与已配对段落相邻时，以纯文本信号沿同一目标扩展
    changed = True
    while changed:
        changed = False
        new_targets: dict[int, set[int]] = {}
        for lo, ln, _s in edges:
            new_targets.setdefault(ln, set()).add(lo)
        for ln in gap_n:
            if ln in paired_n:
                continue
            best: Optional[tuple[float, int]] = None
            for adj in (ln - 1, ln + 1):
                for lo in new_targets.get(adj, ()):
                    sig = _attach_signal(
                        new_block[ln], old_block[lo],
                        split=True, adjacent=False,
                    )
                    if sig >= CHAIN_ATTACH and (
                        best is None or sig > best[0]
                    ):
                        best = (sig, lo)
            if best:
                add_edge(best[1], ln)
                changed = True

        old_targets: dict[int, set[int]] = {}
        for lo, ln, _s in edges:
            old_targets.setdefault(lo, set()).add(ln)
        for lo in gap_o:
            if lo in paired_o:
                continue
            best = None
            for adj in (lo - 1, lo + 1):
                for ln in old_targets.get(adj, ()):
                    sig = _attach_signal(
                        old_block[lo], new_block[ln],
                        split=False, adjacent=False,
                    )
                    if sig >= CHAIN_ATTACH and (
                        best is None or sig > best[0]
                    ):
                        best = (sig, ln)
            if best:
                add_edge(lo, best[1])
                changed = True

    return edges


def _components(
    n_old: int, n_new: int, edges: list[tuple[int, int, float]]
) -> list[dict[str, Any]]:
    """并查集聚类：成边节点归入同一变更分量；孤立节点各自成单节点分量。"""
    parent = list(range(n_old + n_new))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    edge_map: dict[tuple[int, int], float] = {}
    for oi, ni, sim in edges:
        union(oi, n_old + ni)
        edge_map[(oi, ni)] = sim

    groups: dict[int, dict[str, Any]] = {}

    def slot(root: int) -> dict[str, Any]:
        return groups.setdefault(root, {"old": set(), "new": set()})

    for oi in range(n_old):
        slot(find(oi))["old"].add(oi)
    for ni in range(n_new):
        slot(find(n_old + ni))["new"].add(ni)

    out = []
    for g in groups.values():
        g["edges"] = {
            (oi, ni): edge_map[(oi, ni)]
            for oi in g["old"]
            for ni in g["new"]
            if (oi, ni) in edge_map
        }
        out.append(g)
    return out


def _refine_replace(
    old_block: list[str],
    new_block: list[str],
    old_off: int,
    new_off: int,
) -> list[dict[str, Any]]:
    edges, mat = _block_edges(old_block, new_block)
    components = _components(len(old_block), len(new_block), edges)
    components.sort(
        key=lambda g: (
            min(g["old"]) if g["old"] else 10**9,
            min(g["new"]) if g["new"] else 10**9,
        )
    )

    rows: list[dict[str, Any]] = []
    for g in components:
        oi = [old_off + x + 1 for x in sorted(g["old"])]
        ni = [new_off + x + 1 for x in sorted(g["new"])]
        sims = list(g["edges"].values())
        n_o, n_n = len(oi), len(ni)
        mean_sim = round(sum(sims) / len(sims), 3) if sims else 0.0
        max_sim = round(max(sims), 3) if sims else 0.0

        if n_o >= 1 and n_n == 0:
            best = round(
                max(
                    (mat[lo][ln]
                     for ln in range(len(new_block))
                     for lo in g["old"]),
                    default=0.0,
                ),
                3,
            )
            rows.append(
                _row(
                    oi, [], DELETED, _confidence(best, DELETED),
                    {
                        "summary": (
                            f"旧版第 {_join_idx(oi)} 段在新版对应区域无相似段落"
                            f"（块内最高相似度 {best:.2f}），判定为删除。"
                        ),
                        "evidence": {
                            "rule": "deleted_no_pair_in_replace",
                            "best_new_similarity": best,
                        },
                    },
                )
            )
        elif n_o == 0 and n_n >= 1:
            best = round(
                max(
                    (mat[lo][ln]
                     for lo in range(len(old_block))
                     for ln in g["new"]),
                    default=0.0,
                ),
                3,
            )
            rows.append(
                _row(
                    [], ni, ADDED, _confidence(best, ADDED),
                    {
                        "summary": (
                            f"新版第 {_join_idx(ni)} 段在旧版对应区域无相似段落"
                            f"（块内最高相似度 {best:.2f}），判定为新增。"
                        ),
                        "evidence": {
                            "rule": "added_no_pair_in_replace",
                            "best_old_similarity": best,
                        },
                    },
                )
            )
        elif n_o == 1 and n_n == 1:
            sim = sims[0]
            change = UNCHANGED if sim >= MATCH_THRESHOLD else REWRITTEN
            if change == UNCHANGED:
                summary = (
                    f"旧版第 {oi[0]} 段与新版第 {ni[0]} 段高度一致"
                    f"（相似度 {sim:.2f}），判定为无变更。"
                )
                rule = "one_to_one_match"
            else:
                summary = (
                    f"旧版第 {oi[0]} 段与新版第 {ni[0]} 段一一对应，"
                    f"但文字存在差异（相似度 {sim:.2f}，低于无变更阈值"
                    f" {MATCH_THRESHOLD:.2f}），判定为改写。"
                )
                rule = "one_to_one_rewrite"
            rows.append(
                _row(
                    oi, ni, change, _confidence(sim, change),
                    {
                        "summary": summary,
                        "evidence": {"rule": rule, "similarity": sim},
                    },
                )
            )
        elif n_o == 1 and n_n > 1:
            if mean_sim >= SPLIT_MEAN_THRESHOLD:
                rows.append(
                    _row(
                        oi, ni, SPLIT, _confidence(mean_sim, SPLIT),
                        {
                            "summary": (
                                f"旧版第 {oi[0]} 段的内容在新版中分散到第 "
                                f"{_join_idx(ni)} 段（共 {n_n} 段），平均相似度 "
                                f"{mean_sim:.2f}，判定为拆分。"
                            ),
                            "evidence": {
                                "rule": "one_to_many_split",
                                "pair_similarities": _pair_evidence(
                                    g["edges"], old_off, new_off
                                ),
                                "mean_similarity": mean_sim,
                                "max_similarity": max_sim,
                            },
                        },
                    )
                )
            else:
                rows.extend(_split_as_delete_add(oi, ni, mean_sim))
        elif n_o > 1 and n_n == 1:
            rows.append(
                _row(
                    oi, ni, MERGED, _confidence(mean_sim, MERGED),
                    {
                        "summary": (
                            f"旧版第 {_join_idx(oi)} 段（共 {n_o} 段）在新版中"
                            f"合并为第 {ni[0]} 段，平均相似度 {mean_sim:.2f}，"
                            f"判定为合并。"
                        ),
                        "evidence": {
                            "rule": "many_to_one_merge",
                            "pair_similarities": _pair_evidence(
                                g["edges"], old_off, new_off
                            ),
                            "mean_similarity": mean_sim,
                            "max_similarity": max_sim,
                        },
                    },
                )
            )
        else:
            rows.append(
                _row(
                    oi, ni, REWRITTEN, _confidence(mean_sim, REWRITTEN),
                    {
                        "summary": (
                            f"旧版 {n_o} 段（第 {_join_idx(oi)} 段）与新版 "
                            f"{n_n} 段（第 {_join_idx(ni)} 段）多对多重写，"
                            f"平均相似度 {mean_sim:.2f}、最高 {max_sim:.2f}，"
                            f"判定为改写。"
                        ),
                        "evidence": {
                            "rule": "many_to_many_rewrite",
                            "pair_similarities": _pair_evidence(
                                g["edges"], old_off, new_off
                            ),
                            "mean_similarity": mean_sim,
                            "max_similarity": max_sim,
                        },
                    },
                )
            )
    return rows


def _split_as_delete_add(
    oi: list[int], ni: list[int], mean_sim: float
) -> list[dict[str, Any]]:
    """1:N 但平均相似度过低：不强行判拆分，退化为删除 + 新增候选。"""
    rows = [
        _row(
            oi, [], DELETED, _confidence(mean_sim, DELETED),
            {
                "summary": (
                    f"旧版第 {oi[0]} 段虽与新版多段有微弱相似（平均 "
                    f"{mean_sim:.2f}，低于拆分阈值 {SPLIT_MEAN_THRESHOLD:.2f}），"
                    f"仍按删除处理；如确为拆分可在审阅时手动调整。"
                ),
                "evidence": {
                    "rule": "split_below_threshold",
                    "mean_similarity": mean_sim,
                },
            },
        )
    ]
    for x in ni:
        rows.append(
            _row(
                [], [x], ADDED, _confidence(mean_sim, ADDED),
                {
                    "summary": (
                        f"新版第 {x} 段与旧版内容相似度过低（平均 {mean_sim:.2f}，"
                        f"低于拆分阈值 {SPLIT_MEAN_THRESHOLD:.2f}），按新增处理；"
                        f"如确为拆分可在审阅时手动调整。"
                    ),
                    "evidence": {
                        "rule": "split_below_threshold",
                        "mean_similarity": mean_sim,
                    },
                },
            )
        )
    return rows


# ---------------------------------------------------------------- 手动重判

def reclassify_manual(
    old_paragraphs: list[str],
    new_paragraphs: list[str],
    old_idx: list[int],
    new_idx: list[int],
    explicit_type: Optional[str] = None,
) -> dict[str, Any]:
    """审阅者手动调整映射后，重新计算变更类型、置信度与依据。

    explicit_type 非空时以审阅者选择为准（仍计算相似度作为佐证）。
    """
    n_o, n_n = len(old_idx), len(new_idx)
    if n_o == 0 and n_n == 0:
        raise ValueError("旧版与新版映射不能同时为空")

    pair_sims: dict[tuple[int, int], float] = {}
    sims: list[float] = []
    for o in old_idx:
        for n in new_idx:
            s = similarity(old_paragraphs[o - 1], new_paragraphs[n - 1])
            pair_sims[(o, n)] = round(s, 3)
            sims.append(s)
    mean_sim = round(sum(sims) / len(sims), 3) if sims else 0.0
    max_sim = round(max(sims), 3) if sims else 0.0

    if explicit_type:
        if explicit_type not in (
            UNCHANGED, ADDED, DELETED, SPLIT, MERGED, REWRITTEN
        ):
            raise ValueError(f"未知变更类型: {explicit_type}")
        change_type = explicit_type
    elif n_o == 0:
        change_type = ADDED
    elif n_n == 0:
        change_type = DELETED
    elif n_o == 1 and n_n == 1:
        change_type = UNCHANGED if mean_sim >= MATCH_THRESHOLD else REWRITTEN
    elif n_o == 1:
        change_type = SPLIT
    elif n_n == 1:
        change_type = MERGED
    else:
        change_type = REWRITTEN

    rationale = {
        "summary": _manual_summary(
            change_type, old_idx, new_idx, mean_sim, max_sim, explicit_type
        ),
        "evidence": {
            "rule": "manual_mapping",
            "manual": True,
            "pair_similarities": [
                {"old_paragraph": o, "new_paragraph": n, "similarity": s}
                for (o, n), s in sorted(pair_sims.items())
            ],
            "mean_similarity": mean_sim,
            "max_similarity": max_sim,
            "explicit_type": explicit_type,
        },
    }
    return {
        "old_idx": old_idx,
        "new_idx": new_idx,
        "change_type": change_type,
        "confidence": _confidence(mean_sim, change_type, manual=True),
        "rationale": rationale,
    }


def _manual_summary(
    change_type, old_idx, new_idx, mean_sim, max_sim, explicit_type
) -> str:
    label = TYPE_LABELS[change_type]
    source = "审阅者手动指定" if explicit_type else "审阅者调整映射后重新判定"
    if not old_idx:
        return f"{source}为“{label}”：仅映射新版第 {_join_idx(new_idx)} 段。"
    if not new_idx:
        return f"{source}为“{label}”：仅映射旧版第 {_join_idx(old_idx)} 段。"
    return (
        f"{source}为“{label}”：旧版第 {_join_idx(old_idx)} 段 ↔ 新版第 "
        f"{_join_idx(new_idx)} 段，平均相似度 {mean_sim:.2f}，"
        f"最高相似度 {max_sim:.2f}。"
    )


# ---------------------------------------------------------------- 辅助函数

def _row(
    old_idx, new_idx, change_type, confidence, rationale
) -> dict[str, Any]:
    return {
        "old_idx": old_idx,
        "new_idx": new_idx,
        "change_type": change_type,
        "confidence": round(float(confidence), 3),
        "rationale": rationale,
    }


def _confidence(sim: float, change_type: str, manual: bool = False) -> float:
    """由相似度映射为置信度。

    映射类（无变更/改写/拆分/合并）随相似度升高；删除/新增随相似度降低
    （对面版本确实没有对应内容才可信）。手动调整时整体小幅下修，提示
    该结果来自人工映射而非自动算法。
    """
    sim = max(0.0, min(1.0, sim))
    if change_type in (UNCHANGED, REWRITTEN, SPLIT, MERGED):
        conf = 0.45 + 0.5 * sim
    elif change_type in (DELETED, ADDED):
        conf = 0.95 - 0.55 * sim
    else:
        conf = sim
    if manual:
        conf = min(0.99, conf * 0.9 + 0.05)
    return round(conf, 3)


def _pair_evidence(
    edges: dict[tuple[int, int], float],
    old_off: int = 0,
    new_off: int = 0,
) -> list[dict[str, Any]]:
    return [
        {
            "old_paragraph": old_off + lo + 1,
            "new_paragraph": new_off + ln + 1,
            "similarity": round(s, 3),
        }
        for (lo, ln), s in sorted(edges.items())
    ]


def _join_idx(indices: list[int]) -> str:
    return "、".join(str(i) for i in indices)


def _order_rows(
    rows: list[dict[str, Any]], n_old: int, n_new: int
) -> list[dict[str, Any]]:
    """按文档顺序输出：双指针交替取覆盖下一个旧段/新段的行。"""
    remaining = list(rows)
    ordered: list[dict[str, Any]] = []
    o_cur = n_cur = 1
    while remaining:
        choice = None
        for r in remaining:
            if o_cur in r["old_idx"]:
                choice = r
                break
        if choice is None:
            for r in remaining:
                if n_cur in r["new_idx"]:
                    choice = r
                    break
        if choice is None:
            choice = min(
                remaining,
                key=lambda r: (
                    min(r["old_idx"], default=10**9),
                    min(r["new_idx"], default=10**9),
                ),
            )
        ordered.append(choice)
        remaining.remove(choice)
        covered_o = [i for i in choice["old_idx"] if i >= o_cur]
        covered_n = [i for i in choice["new_idx"] if i >= n_cur]
        if covered_o:
            o_cur = max(covered_o) + 1
        if covered_n:
            n_cur = max(covered_n) + 1
    return ordered
