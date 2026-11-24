"""Subgroup and fairness metrics for the champion credit risk model.

    KUBEML_DATA_DIR=/tmp/kfair .venv/bin/python scripts/fairness_report.py --rows 20000 --seed 2024 \
        --out docs/evidence/fairness/fairness_report.json

Age is a protected characteristic under the UK Equality Act 2010, and the synthetic
generator gives it a small effect on default risk, so it is the main attribute here.
Housing is reported as a business segment. The data is synthetic, so this shows the
audit method, not a finding about real borrowers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from kubeml.config import get_settings
from kubeml.dataset import generate_frame
from kubeml.schema import FEATURE_NAMES, TARGET
from kubeml.training import train_credit_risk


def group_metrics(y: np.ndarray, score: np.ndarray, threshold: float = 0.5) -> dict:
    pred = score >= threshold
    pos, neg = y == 1, y == 0
    return {
        "rows": int(len(y)),
        "default_rate": float(y.mean()),
        "flagged_rate": float(pred.mean()),
        "true_positive_rate": float(pred[pos].mean()) if pos.any() else None,
        "false_positive_rate": float(pred[neg].mean()) if neg.any() else None,
        "precision": float(y[pred].mean()) if pred.any() else None,
        "roc_auc": float(roc_auc_score(y, score)) if len(set(y)) == 2 else None,
        "mean_score": float(score.mean()),
        "calibration_gap": float(score.mean() - y.mean()),
    }


def audit(frame: pd.DataFrame, score: np.ndarray, column: str) -> dict:
    y = frame[TARGET].to_numpy()
    groups = {
        str(k): group_metrics(y[idx], score[idx])
        for k, idx in frame.groupby(column, observed=True).indices.items()
    }

    def spread(key):
        vals = [g[key] for g in groups.values() if g[key] is not None]
        return float(max(vals) - min(vals))

    flagged = [g["flagged_rate"] for g in groups.values()]
    return {
        "groups": groups,
        "demographic_parity_difference": spread("flagged_rate"),
        "disparate_impact_ratio": float(min(flagged) / max(flagged)) if max(flagged) > 0 else None,
        "equal_opportunity_difference": spread("true_positive_rate"),
        "equalized_odds_difference": max(
            spread("true_positive_rate"), spread("false_positive_rate")
        ),
        "auc_spread": spread("roc_auc"),
        "calibration_gap_spread": spread("calibration_gap"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--out", type=Path, default=Path("docs/evidence/fairness/fairness_report.json"))
    args = ap.parse_args()
    trained = train_credit_risk(get_settings())
    model = trained["model"]
    frame = generate_frame(args.rows, args.seed, "reference")
    score = np.array([p["score"] for p in model.predict(frame[FEATURE_NAMES])])
    frame["age_band"] = pd.cut(frame["age"], [0, 34, 49, 120], labels=["22-34", "35-49", "50-73"])
    # Counterfactual: score everyone as if they were 45, to separate the direct use of age
    # from differences in the other features.
    neutral = frame[FEATURE_NAMES].copy()
    neutral["age"] = 45
    score_neutral = np.array([p["score"] for p in model.predict(neutral)])
    report = {
        "rows": args.rows,
        "seed": args.seed,
        "threshold": 0.5,
        "overall": group_metrics(frame[TARGET].to_numpy(), score),
        "age_band": audit(frame, score, "age_band"),
        "age_band_if_age_fixed_at_45": audit(frame, score_neutral, "age_band"),
        "housing": audit(frame, score, "housing"),
        "note": "Synthetic data. The generator lowers default risk by 0.02 on the logit scale per year of age.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {m: v for m, v in report[k].items() if m != "groups"}
                for k in ("age_band", "age_band_if_age_fixed_at_45", "housing")
            },
            indent=1,
        )
    )
    for k in ("age_band", "age_band_if_age_fixed_at_45", "housing"):
        print(pd.DataFrame(report[k]["groups"]).T.round(3).to_string())


if __name__ == "__main__":
    main()
