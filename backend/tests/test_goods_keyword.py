"""商品关键词检索的**纯函数**测试 —— 不连库、不调模型。

来历（2026-10-21 实测翻车）：
卖家问「联想笔记本电脑与**英伟达5090显卡**差多少钱」，库里那件叫
**「英伟达RTX5090显卡」**（G-0012，就排在商品管理列表第一行）。
当时的检索是整串 `title LIKE '%英伟达5090显卡%'` → 0 条 → 工具回
「没有符合条件的商品」→ 助手据此对卖家断言「咱们店里目前并没有售卖这款商品」。

**数据没错，是检索把「这个词没命中」说成了「库里没有」。**
修法：把搜索词切成「连续中文 / 连续字母数字」片段，逐段 AND 命中。

  K 组 切段：中英混排 / 中文标点 / 去重 / 空串
  L 组 SQL 片段：条件数与参数个数、AND 而非 OR、通配符转义
  R 组 回归：那条真实商品标题必须被每一段命中（这条挂了就说明退化回整串匹配）

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_goods_keyword.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.goods_repo import _like, keyword_terms, keyword_where  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def like_hit(text: str, pattern: str) -> bool:
    """把 `%X%` 当子串匹配来模拟（测试不连库）。先还原 `\\%` / `\\_` 转义。"""
    inner = pattern[1:-1] if pattern.startswith("%") and pattern.endswith("%") else pattern
    return inner.replace("\\%", "%").replace("\\_", "_") in text


# ============================================================
# K 组：切段
# ============================================================


def test_split() -> None:
    check("K1 中英混排切成三段", keyword_terms("英伟达5090显卡") == ["英伟达", "5090", "显卡"],
          str(keyword_terms("英伟达5090显卡")))
    check("K2 型号字母与数字连写视为一段",
          keyword_terms("英伟达RTX5090显卡") == ["英伟达", "RTX5090", "显卡"],
          str(keyword_terms("英伟达RTX5090显卡")))
    check("K3 纯中文不多切", keyword_terms("联想笔记本电脑") == ["联想笔记本电脑"],
          str(keyword_terms("联想笔记本电脑")))
    # 中文标点必须丢掉 —— 把「，」当成片段会让整条查询必然 0 条
    check("K4 中文标点被丢弃", keyword_terms("凉拖鞋，篮球") == ["凉拖鞋", "篮球"],
          str(keyword_terms("凉拖鞋，篮球")))
    check("K5 去重保序", keyword_terms("显卡 显卡 RTX ") == ["显卡", "RTX"],
          str(keyword_terms("显卡 显卡 RTX ")))
    check("K6 空串与纯标点无片段", keyword_terms("") == [] and keyword_terms("  ，。 ") == [],
          f"{keyword_terms('')} / {keyword_terms('  ，。 ')}")
    check("K7 编号原样保留", keyword_terms("G-0012") == ["G", "0012"], str(keyword_terms("G-0012")))


# ============================================================
# L 组：SQL 片段
# ============================================================


def test_where() -> None:
    clause, params = keyword_where("英伟达5090显卡")
    check("L1 三段 → 三个条件、六个参数",
          clause.count("LIKE") == 6 and len(params) == 6, f"{clause} / {params}")
    check("L2 片段之间是 AND（不是 OR）",
          clause.count(" OR ") == 3 and clause.count(" AND ") == 2, clause)
    check("L3 每个条件都同时看标题与编号",
          clause.count("(title LIKE %s OR goods_no LIKE %s)") == 3, clause)
    check("L4 空关键词不产生条件",
          keyword_where("") == ("", []), str(keyword_where("")))

    # 卖家在搜索框输个 % 就等于要全表 —— 两道防线都要有：
    #   ① 切段只认「中文 / 字母数字」，通配符在进入 SQL 之前就被剥掉；
    #   ② 万一将来放宽切段正则，`_like` 仍会把它们转义成字面量。
    check("L5 通配符在切段阶段就被剥离",
          keyword_terms("100%") == ["100"] and keyword_terms("a_b") == ["a", "b"],
          f"{keyword_terms('100%')} / {keyword_terms('a_b')}")
    check("L6 转义函数本身也把通配符变字面量",
          _like("a%b_c\\d") == "%a\\%b\\_c\\\\d%", _like("a%b_c\\d"))


# ============================================================
# R 组：真实数据的回归
# ============================================================


def test_regression() -> None:
    """库里那条真实标题，必须被卖家可能的每一种说法命中 —— 这是整次修复的目的。"""
    title = "英伟达RTX5090显卡"
    for spoken in ("英伟达5090显卡", "英伟达RTX5090显卡", "5090显卡", "英伟达 显卡", "RTX5090"):
        clause, params = keyword_where(spoken)
        patterns = params[0::2]          # 标题那一侧的参数
        ok = bool(patterns) and all(like_hit(title, p) for p in patterns)
        check(f"R1 「{spoken}」能命中「{title}」", ok, f"未命中：{patterns}")

    # 整串匹配的老行为必须**已经失效**：这条断言就是"没有退化回去"的钉子
    old_ok = "英伟达5090显卡" in title
    check("R2 整串匹配本来就命中不了（证明 R1 不是恒真）", not old_ok, f"整串匹配={old_ok}")

    # 但也不能变成越搜越多：不相干的词不能命中
    _, params = keyword_where("电饭煲")
    check("R3 不相干词不命中", not any(like_hit(title, p) for p in params[0::2]), str(params))


def main() -> None:
    test_split()
    test_where()
    test_regression()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
