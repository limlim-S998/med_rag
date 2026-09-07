# TF-IDF + LinearSVC over table titles and header stacks.
#
# Why not ask the LLM to classify the table:
#   deterministic (same input, same label, every time - auditable)
#   milliseconds instead of ~1s
#   free per call, and this runs on every table in a 300-page TFL package
#   retrainable when a new table shell appears
#
# And it gives continuous training an object. If the generative path is
# retrieval + prompting and nothing is fine-tuned, the CT in CI/CD/CT has
# nothing to retrain - except this.

import mlflow
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import classification_report
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC


def build_pipeline() -> Pipeline:
    return Pipeline([
        ("tfidf", TfidfVectorizer(
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=2,
            strip_accents="unicode",
        )),
        # Calibrated so the serving path can abstain: below a probability
        # threshold, fall back to the generic template rather than confidently
        # rendering an AE narrative for an efficacy table.
        ("clf", CalibratedClassifierCV(LinearSVC(C=1.0, class_weight="balanced"), cv=5)),
    ])


def main(X, y, run_name: str = "table-clf") -> None:
    # Azure ML tracking is just an MLflow tracking URI. Everything you already
    # know about MLflow transfers; `az ml workspace show --query mlflow_tracking_uri`
    # gives you the string.
    mlflow.set_experiment("table-type-classification")
    with mlflow.start_run(run_name=run_name):
        pipe = build_pipeline()
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=0)
        preds = cross_val_predict(pipe, X, y, cv=cv)
        report = classification_report(y, preds, output_dict=True)
        mlflow.log_metric("macro_f1", report["macro avg"]["f1-score"])
        for label, m in report.items():
            if isinstance(m, dict):
                mlflow.log_metric(f"f1_{label}", m["f1-score"])

        pipe.fit(X, y)
        mlflow.sklearn.log_model(
            pipe, artifact_path="model",
            registered_model_name="table-type-classifier",
        )

# This is the honest answer to "why have a model registry at all if GPT-4o is
# an API call": hosted models have no artifacts, so their versioning lives in
# Helm values. The registry earns its keep on the models that do have weights
# you trained.
