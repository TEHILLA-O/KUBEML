# Model card: KubeML credit risk model

| | |
| --- | --- |
| Model | L2 logistic regression on CPU, coefficients stored as JSON (no pickles) |
| Task | Binary classification: `default` or `no_default` |
| Inputs | 8 numeric features plus `housing` and `loan_purpose` (`credit-risk.features.v1`) |
| Training data | Synthetic credit book from `src/kubeml/dataset.py`, 4,000 rows, seed 7, 70/15/15 split |
| Versioning | Every training run is logged to MLflow with the dataset fingerprint, artifact SHA256 and git commit, and registered as a new version of `credit-risk` |
| Release gate | Validation ROC AUC at least 0.70, plus data quality, baseline regression, integrity and smoke gates |

## Performance (synthetic data)

| Split | Samples | Accuracy | F1 | ROC AUC | Brier |
| --- | --- | --- | --- | --- | --- |
| Validation | 600 | 0.867 | 0.630 | 0.891 | 0.098 |
| Test | 600 | 0.872 | 0.675 | 0.910 | 0.090 |

Metrics are also broken down by `housing` and `loan_purpose` segment in each run's evaluation (see `CreditRiskModel.evaluate`).

## Drift and retraining

`kubeml-retrain` checks a new labelled batch for drift (PSI, KS, JS) and for a drop in the champion's ROC AUC, and retrains when at least 2 features drift or AUC falls by more than 0.03. Results from 24 November 2026 are in the README section "Drift-triggered retraining" and in `docs/evidence/retraining/`. A retrained model is registered as a `candidate` version and is never promoted automatically.

## Limitations

- The data is synthetic, so these numbers say nothing about real lending.
- The synthetic drift scenarios change the inputs but not the rule that links inputs to defaults, so retraining gains are small (about 0.001 ROC AUC on the drifted holdout).
- `housing` and `loan_purpose` are the only segments. There are no protected attributes, so no fairness analysis is possible.
