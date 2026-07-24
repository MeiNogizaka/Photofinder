"""dataset_export.py が作る書き出しzip (POST /api/export/dataset) を解析し、
AI自動タグ (物体検出/種名/色) の精度を客観的に評価する。

使い方:
    .venv/bin/python tools/evaluate_dataset.py dataset-20260725-120000.zip

重要な前提: このスクリプトが見られるのは photo_tags.verified (人手の確定/否認)
だけであり、真の正解ラベルは知らない。「モデルが提案したものに人間がどれだけ
同意したか」の集計であり、モデルが検出し損ねたもの (false negative) は
原理的に測れない — 精度の傾向を掴む参考値として使うこと。
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import defaultdict

# photofinder/detector.py の CONF_THRESHOLD と同じ値。閾値スイープ表で
# 「現在の設定」の位置が分かるように印を付ける
CURRENT_YOLO_CONF_THRESHOLD = 0.35
YOLO_SWEEP_THRESHOLDS = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80]


def load_rows(zip_path: str) -> list[dict]:
    try:
        with zipfile.ZipFile(zip_path) as z:
            text = z.read("dataset.jsonl").decode("utf-8")
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        sys.exit(f"エラー: {zip_path} を開けません ({e})")
    except KeyError:
        sys.exit(f"エラー: {zip_path} に dataset.jsonl が見つかりません "
                  "(POST /api/export/dataset の出力ではない可能性)")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _precision_row(label: str, pos: int, neg: int, extra: str = "") -> str:
    total = pos + neg
    prec = f"{pos / total:>6.1%}" if total else "   N/A"
    return f"{label:<12} {pos:>6} {neg:>6} {prec}{extra}"


def print_source_precision(rows: list[dict]) -> None:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        for t in row.get("positive_tags", []):
            counts[t["source"]][0] += 1
        for t in row.get("negative_tags", []):
            counts[t["source"]][1] += 1
    print("\n=== タグ種別 (source) ごとの確定/否認 ===")
    print(f"{'source':<12} {'確定':>6} {'否認':>6} {'精度':>7}")
    for source, (pos, neg) in sorted(counts.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        print(_precision_row(source, pos, neg))


def print_yolo_threshold_sweep(rows: list[dict]) -> None:
    yolo_pos = [t["conf"] for row in rows for t in row.get("positive_tags", [])
                if t["source"] == "yolo"]
    yolo_neg = [t["conf"] for row in rows for t in row.get("negative_tags", [])
                if t["source"] == "yolo"]
    print("\n=== YOLO信頼度しきい値の感度分析 (photofinder/detector.py CONF_THRESHOLD) ===")
    if not yolo_pos and not yolo_neg:
        print("(source=yoloの確定/否認タグが無いためスキップ)")
        return
    print("そのしきい値以上だけ残した場合に、レビュー済みタグがどう変化するか:")
    print(f"{'閾値':>6} {'残る確定':>8} {'残る否認':>8} {'精度':>7}")
    for thr in YOLO_SWEEP_THRESHOLDS:
        pos = sum(1 for c in yolo_pos if c >= thr)
        neg = sum(1 for c in yolo_neg if c >= thr)
        total = pos + neg
        prec = f"{pos / total:>6.1%}" if total else "   N/A"
        marker = "  ← 現在の設定" if abs(thr - CURRENT_YOLO_CONF_THRESHOLD) < 1e-9 else ""
        print(f"{thr:>6.2f} {pos:>8} {neg:>8} {prec}{marker}")


def print_species_margin_analysis(rows: list[dict]) -> None:
    pos_margins, neg_margins = [], []
    for row in rows:
        for d in row.get("species_detail", []):
            topk = d.get("topk") or []
            if len(topk) < 2:
                continue
            pos_margins_list = pos_margins if d["verified"] == 1 else neg_margins
            pos_margins_list.append(topk[0][1] - topk[1][1])

    print("\n=== 種名候補のマージン (top1-top2類似度) 分析 (photofinder/bird.py MIN_MARGIN) ===")
    if not pos_margins and not neg_margins:
        print("(species_detailが無いためスキップ)")
        return

    def stats(xs: list[float], label: str) -> None:
        if not xs:
            print(f"  {label}: データなし")
            return
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        print(f"  {label}: n={n}  平均={sum(xs)/n:.4f}  最小={xs_sorted[0]:.4f}  "
              f"中央値={xs_sorted[n // 2]:.4f}  最大={xs_sorted[-1]:.4f}")

    stats(pos_margins, "確定 (人が種名を正しいと認めた)")
    stats(neg_margins, "否認 (人が種名を間違いと判定)")
    print("  → 否認側のマージンの方が全体的に小さいなら、MIN_MARGINを上げる余地がある")


def print_species_reject_rate(rows: list[dict]) -> None:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        for t in row.get("positive_tags", []):
            if t.get("kind") == "species":
                counts[t["name"]][0] += 1
        for t in row.get("negative_tags", []):
            if t.get("kind") == "species":
                counts[t["name"]][1] += 1
    if not counts:
        return
    print("\n=== 種名ごとの確定/否認 (否認が多い順) ===")
    print(f"{'種名':<12} {'確定':>6} {'否認':>6} {'否認率':>7}")
    for name, (pos, neg) in sorted(counts.items(), key=lambda kv: -kv[1][1]):
        total = pos + neg
        print(f"{name:<12} {pos:>6} {neg:>6} {neg / total:>6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="人手タグ付けデータセットの精度を客観的に評価する")
    ap.add_argument("zip_path", help="POST /api/export/dataset で取得したzipファイル")
    args = ap.parse_args()

    rows = load_rows(args.zip_path)
    n_pos = sum(len(r.get("positive_tags", [])) for r in rows)
    n_neg = sum(len(r.get("negative_tags", [])) for r in rows)
    print(f"対象: 写真{len(rows)}枚、確定タグ{n_pos}件、否認タグ{n_neg}件")
    print("注意: ここで測れるのは「人手レビュー済みのものへの一致率」のみ。")
    print("      モデルが検出し損ねたもの (false negative) は測れない。")
    if not rows:
        return

    print_source_precision(rows)
    print_yolo_threshold_sweep(rows)
    print_species_margin_analysis(rows)
    print_species_reject_rate(rows)


if __name__ == "__main__":
    main()
