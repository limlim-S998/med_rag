# Loading a trained model out of the Azure ML registry, by pinned version.
#
# This is the honest answer to "why have a model registry at all if GPT-4o is
# an API call". Hosted models have no artifact: their version lives in a Helm
# value because there is nothing else to version. The registry earns its keep
# on the models that do have weights we trained - which here is exactly one
# that is load-bearing (the table-type classifier) and one that is a baseline.
#
# Never `latest`. The classifier picks which table-to-text template fires,
# which changes the prose. A model that rolls forward on its own is a silent
# behaviour change with no commit anywhere - the same failure mode as an
# unpinned Azure OpenAI deployment name, for the same reason.

import mlflow

from medw_core.settings import Settings

_cache: dict[str, object] = {}


def load_table_classifier(s: Settings):
    """models:/table-type-classifier/7 — pinned, cached for the process life."""
    uri = f"models:/{s.table_classifier_name}/{s.table_classifier_version}"
    if uri not in _cache:
        # Azure ML's tracking store is MLflow-compatible, so this is the same
        # call it would be anywhere. `az ml workspace show --query
        # mlflow_tracking_uri` gives you the URI; in-cluster it is a setting.
        _cache[uri] = mlflow.sklearn.load_model(uri)
    return _cache[uri]


def classify_table(model, document: str, min_proba: float) -> tuple[str, float]:
    # Calibrated, so abstention is meaningful. Below the threshold, return the
    # generic template rather than confidently rendering an AE narrative for
    # an efficacy table - a wrong template is a worse failure than a bland one.
    proba = model.predict_proba([document])[0]
    idx = proba.argmax()
    label = model.classes_[idx]
    return (label if proba[idx] >= min_proba else "other"), float(proba[idx])


# The label AND its confidence AND the model version are stored on the
# document row (core.document.doc_type_proba, classifier_ver). A label with no
# provenance is not auditable, and this label decides how numbers get narrated.
