"""
Register the trained churn model in DataHub, with lineage back to its features.

Why this file exists
--------------------
`train_churn_model.py` produces a real model and writes
`substrate/ml/artifacts/model_metadata.json`. Until now nothing carried that
into the catalog, which left the project's most-quoted sentence — *"blast radius
terminates at a registered ML model"* — resting on an entity no committed script
created. `backend/v2/ml_impact.py` was written, tested and honest about it: its
row in the README carried the caveat **"not yet executed against a live
catalog"**. This is the missing half.

The graph shape, and why it is not the obvious one
--------------------------------------------------
The obvious modelling is `MLModelProperties.trainingData` → the feature table.
It is also the wrong one for this purpose: **`trainingData` produces no
traversable graph edge**, so a blast radius walked from `raw.users` stops at the
mart and never reaches the model. DataHub's traversable path runs through a job:

    dataset ──Consumes──▶ dataJob ──TrainedBy──▶ mlModel
    (user_order_features)  (train_churn_model)   (devguard_churn_risk)

`DataJobInputOutput.inputDatasets` and `MLModelProperties.trainingJobs` are both
annotated as relationships in DataHub's PDL, which is what makes this path
walkable by `searchAcrossLineage` — and therefore by DevGuard's Pathfinder. That
is exactly the finding `ml_impact.py` encodes ("the agent walks entity *types*
instead of assuming the last hop is a dataset"), now with a live graph behind it.

Nothing here is invented
------------------------
Every number written to the model — accuracy, row counts, feature list,
estimator, framework — is read from `model_metadata.json`, which
`train_churn_model.py` wrote from an actual fit. If the training run has not
happened, this refuses rather than registering a model with placeholder metrics.

    python substrate/ml/train_churn_model.py     # produces the metadata
    python substrate/ml/register_model.py        # registers it
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Optional

from datahub.emitter.mce_builder import (
    make_data_flow_urn,
    make_data_job_urn_with_flow,
    make_dataset_urn,
    make_group_urn,
    make_ml_model_urn,
    make_user_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    CorpGroupInfoClass,
    DataFlowInfoClass,
    DataJobInfoClass,
    DataJobInputOutputClass,
    MLHyperParamClass,
    MLMetricClass,
    MLModelPropertiesClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    VersionTagClass,
)

ARTIFACT = pathlib.Path("substrate/ml/artifacts/model_metadata.json")

#: The platform the model is registered under. `mlflow` is a DataHub-known
#: platform; the model is not *in* MLflow, and the description says so rather
#: than letting the platform imply a system that is not running.
MODEL_PLATFORM = "mlflow"

#: The orchestration identity of the training run. A dataFlow is required
#: because a dataJob URN embeds one — there is no "job without a flow" in
#: DataHub's model.
FLOW_ORCHESTRATOR = "devguard"
FLOW_ID = "churn_training"
JOB_ID = "train_churn_model"

#: The feature table the model reads. Postgres platform, because that is the
#: physical table `train_churn_model.py` connects to — the dbt sibling describes
#: the same table but is not what the training run opens.
FEATURE_TABLE_URN = make_dataset_urn(
    "postgres", "devguard.analytics_marts.user_order_features", "PROD"
)

#: The model's owning team. Created here as a CorpGroup rather than reusing the
#: dataset's owner **on purpose**: `ml_impact.py` exists because the person who
#: owns a feature table is very often not the person on the hook for the model
#: trained on it, and routing an incident only to the dataset owner leaves the
#: model owner unaware their training data moved. That claim is only
#: demonstrable if the graph actually disagrees, so the graph actually
#: disagrees.
ML_OWNER_GROUP = "devguard_ml_platform"


def load_metadata() -> dict:
    if not ARTIFACT.exists():
        raise SystemExit(
            f"{ARTIFACT} does not exist. Run `python substrate/ml/train_churn_model.py` "
            "first — this script registers a model that was actually trained, and "
            "will not invent metrics for one that was not."
        )
    return json.loads(ARTIFACT.read_text())


def build_mcps(meta: dict) -> list[MetadataChangeProposalWrapper]:
    model_name = meta["model_name"]
    model_urn = make_ml_model_urn(MODEL_PLATFORM, model_name, "PROD")
    flow_urn = make_data_flow_urn(FLOW_ORCHESTRATOR, FLOW_ID, "PROD")
    job_urn = make_data_job_urn_with_flow(flow_urn, JOB_ID)
    group_urn = make_group_urn(ML_OWNER_GROUP)

    trained_at = meta["trained_at"]
    features = meta["feature_columns"]

    mcps: list[MetadataChangeProposalWrapper] = [
        # --- the owning team -------------------------------------------------
        MetadataChangeProposalWrapper(
            entityUrn=group_urn,
            aspect=CorpGroupInfoClass(
                admins=[], members=[], groups=[],
                displayName="DevGuard ML Platform",
                description=(
                    "Owns the churn model registered from substrate/ml. Created by "
                    "substrate/ml/register_model.py so that model ownership and "
                    "dataset ownership are genuinely different in the graph."
                ),
            ),
        ),
        # --- the training pipeline ------------------------------------------
        MetadataChangeProposalWrapper(
            entityUrn=flow_urn,
            aspect=DataFlowInfoClass(
                name="DevGuard churn training",
                description=(
                    "The training pipeline for the substrate's churn model. One job: "
                    "substrate/ml/train_churn_model.py."
                ),
            ),
        ),
        MetadataChangeProposalWrapper(
            entityUrn=job_urn,
            aspect=DataJobInfoClass(
                name="train_churn_model",
                type="COMMAND",
                description=(
                    "Reads analytics_marts.user_order_features and fits a "
                    f"{meta['estimator']} on {', '.join(features)}. "
                    "This job is the hop that makes the model reachable from the "
                    "feature table by graph traversal."
                ),
            ),
        ),
        # The edge that carries the blast radius into ML territory.
        MetadataChangeProposalWrapper(
            entityUrn=job_urn,
            aspect=DataJobInputOutputClass(
                inputDatasets=[FEATURE_TABLE_URN],
                outputDatasets=[],
                inputDatajobs=[],
            ),
        ),
        # --- the model itself -------------------------------------------------
        MetadataChangeProposalWrapper(
            entityUrn=model_urn,
            aspect=MLModelPropertiesClass(
                name=model_name,
                description=(
                    f"{meta['framework']} {meta['estimator']} predicting "
                    f"`{meta['label']}` from {len(features)} features of "
                    f"{meta['feature_table']}. Trained by "
                    "substrate/ml/train_churn_model.py against the substrate "
                    "Postgres; every metric below is from that run. The model "
                    "artifact is a local file — it is registered under the mlflow "
                    "platform because DataHub knows that platform, not because an "
                    "MLflow server exists."
                ),
                date=None,
                version=VersionTagClass(versionTag=trained_at),
                # `trainingJobs` is the relationship-annotated field. This is the
                # edge `searchAcrossLineage` can walk; `trainingData` is not.
                trainingJobs=[job_urn],
                hyperParams=[
                    MLHyperParamClass(name="estimator", value=meta["estimator"]),
                    MLHyperParamClass(name="framework", value=meta["framework"]),
                    MLHyperParamClass(name="id_column", value=meta["id_column"]),
                    MLHyperParamClass(name="label", value=meta["label"]),
                    MLHyperParamClass(name="features", value=", ".join(features)),
                ],
                trainingMetrics=[
                    MLMetricClass(name="test_accuracy",
                                  value=f"{meta['test_accuracy']:.4f}"),
                    MLMetricClass(name="rows_total", value=str(meta["rows_total"])),
                    MLMetricClass(name="rows_train", value=str(meta["rows_train"])),
                    MLMetricClass(name="rows_test", value=str(meta["rows_test"])),
                ],
                customProperties={
                    "feature_table": meta["feature_table"],
                    "trained_at": trained_at,
                    "registered_by": "substrate/ml/register_model.py",
                },
            ),
        ),
        MetadataChangeProposalWrapper(
            entityUrn=model_urn,
            aspect=OwnershipClass(owners=[
                OwnerClass(owner=group_urn, type=OwnershipTypeClass.TECHNICAL_OWNER),
            ]),
        ),
    ]
    return mcps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the URNs and aspects, emit nothing.")
    args = ap.parse_args()

    meta = load_metadata()
    mcps = build_mcps(meta)

    model_urn = make_ml_model_urn(MODEL_PLATFORM, meta["model_name"], "PROD")
    print(f"model    {model_urn}")
    print(f"features {FEATURE_TABLE_URN}")
    print(f"accuracy {meta['test_accuracy']:.4f} on {meta['rows_test']} test rows")
    print()

    if args.dry_run:
        for mcp in mcps:
            print(f"  {type(mcp.aspect).__name__:28} -> {mcp.entityUrn}")
        print("\n  dry run: nothing emitted")
        return 0

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token: Optional[str] = os.environ.get("DATAHUB_GMS_TOKEN") or None
    token_file = os.environ.get("DATAHUB_TOKEN_FILE")
    if not token and token_file:
        token = pathlib.Path(token_file).read_text().strip()

    emitter = DatahubRestEmitter(gms_server=gms, token=token)
    for mcp in mcps:
        emitter.emit(mcp)
        print(f"  emitted {type(mcp.aspect).__name__:28} -> {mcp.entityUrn}")

    print(f"\n  {len(mcps)} aspects emitted to {gms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
