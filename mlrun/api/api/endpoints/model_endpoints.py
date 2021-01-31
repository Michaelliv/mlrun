import json
from dataclasses import dataclass
from http import HTTPStatus
from os import environ
from typing import List, Optional, Dict, Any

import pandas as pd
from fastapi import APIRouter, Query, Response, Request
from v3io.dataplane import RaiseForStatus

from mlrun.api.schemas import (
    ModelEndpointMetadata,
    ModelEndpointSpec,
    ModelEndpoint,
    ModelEndpointStateList,
    ModelEndpointState,
    Features,
    Metric,
    ObjectStatus,
)
from mlrun.config import config
from mlrun.errors import (
    MLRunConflictError,
    MLRunNotFoundError,
    MLRunInvalidArgumentError,
    MLRunBadRequestError,
    MLRunPreconditionFailedError,
)
from mlrun.utils.helpers import logger
from mlrun.utils.v3io_clients import get_v3io_client, get_frames_client

ENDPOINTS_TABLE_PATH = "model-endpoints"
ENDPOINT_EVENTS_TABLE_PATH = "endpoint-events"
ENDPOINT_TABLE_ATTRIBUTES = [
    "project",
    "model",
    "function",
    "tag",
    "model_class",
    "labels",
    "first_request",
    "last_request",
    "error_count",
    "alert_count",
    "drift_status",
]
ENDPOINT_TABLE_ATTRIBUTES_WITH_FEATURES = ENDPOINT_TABLE_ATTRIBUTES + ["features"]

V3IO_ACCESS_KEY = "V3IO_ACCESS_KEY"
V3IO_API = "V3IO_API"
V3IO_FRAMESD = "V3IO_FRAMESD"


router = APIRouter()


@dataclass
class TimeMetric:
    tsdb_column: str
    metric_name: str
    headers: List[str]

    def transform_df_to_metric(self, data: pd.DataFrame) -> Optional[Metric]:
        if data.empty or self.tsdb_column not in data.columns:
            return None

        values = data[self.tsdb_column].reset_index().to_numpy()
        describe = data[self.tsdb_column].describe().to_dict()

        return Metric(
            name=self.metric_name,
            start_timestamp=str(data.index[0]),
            end_timestamp=str(data.index[-1]),
            headers=self.headers,
            values=[(str(timestamp), float(value)) for timestamp, value in values],
            min=describe["min"],
            avg=describe["mean"],
            max=describe["max"],
        )

    @classmethod
    def from_string(cls, name: str) -> "TimeMetric":
        if name in {"microsec", "latency"}:
            return cls(
                tsdb_column="latency_avg_1s",
                metric_name="average_latency",
                headers=["timestamp", "average"],
            )
        elif name in {"preds", "predictions"}:
            return cls(
                tsdb_column="predictions_per_second_count_1s",
                metric_name="predictions_per_second",
                headers=["timestamp", "count"],
            )
        else:
            raise NotImplementedError(f"Unsupported metric '{name}'")


@router.post(
    "/projects/{project}/model-endpoints/{endpoint_id}/clear",
    status_code=HTTPStatus.NO_CONTENT.value,
)
def clear_endpoint_record(request: Request, project: str, endpoint_id: str):
    """
    Clears endpoint record from KV by endpoint_id
    """

    _verify_endpoint(project, endpoint_id)
    secrets = get_model_endpoint_secrets(request)

    logger.info("Clearing model endpoint table", endpoint_id=endpoint_id)
    client = get_v3io_client(
        endpoint=secrets[V3IO_API], access_key=secrets[V3IO_ACCESS_KEY]
    )
    client.kv.delete(
        container=config.httpdb.model_endpoint_monitoring.container,
        table_path=f"{project}/{ENDPOINTS_TABLE_PATH}",
        key=endpoint_id,
    )
    logger.info("Model endpoint table deleted", endpoint_id=endpoint_id)

    return Response(status_code=HTTPStatus.NO_CONTENT.value)


@router.get(
    "/projects/{project}/model-endpoints", response_model=ModelEndpointStateList
)
def list_endpoints(
    request: Request,
    project: str,
    model: Optional[str] = Query(None),
    function: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    labels: List[str] = Query([], alias="label"),
    start: str = Query(default="now-1h"),
    end: str = Query(default="now"),
    metrics: bool = Query(default=False),
):
    """
    Returns a list of endpoints of type 'ModelEndpoint', supports filtering by model, function, tag and labels.
    Lables can be used to filter on the existance of a label:
    `api/projects/{project}/model-endpoints/?label=mylabel`

    Or on the value of a given label:
    `api/projects/{project}/model-endpoints/?label=mylabel=1`

    Multiple labels can be queried in a single request by either using `&` seperator:
    `api/projects/{project}/model-endpoints/?label=mylabel=1&label=myotherlabel=2`

    Or by using a `,` (comma) seperator:
    `api/projects/{project}/model-endpoints/?label=mylabel=1,myotherlabel=2`
    """
    secrets = get_model_endpoint_secrets(request)
    client = get_v3io_client(
        endpoint=secrets[V3IO_API], access_key=secrets[V3IO_ACCESS_KEY]
    )
    cursor = client.kv.new_cursor(
        container=config.httpdb.model_endpoint_monitoring.container,
        table_path=f"{project}/{ENDPOINTS_TABLE_PATH}",
        attribute_names=ENDPOINT_TABLE_ATTRIBUTES,
        filter_expression=_build_kv_cursor_filter_expression(
            project, function, model, tag, labels
        ),
    )
    endpoints = cursor.all()

    endpoint_state_list = []
    for endpoint in endpoints:
        endpoint_metrics = None
        if metrics:
            endpoint_metrics = _get_endpoint_metrics(
                secrets=secrets,
                project=project,
                endpoint_id=endpoint.get("id"),
                name=["predictions", "latency"],
                start=start,
                end=end,
            )

        # Collect labels (by convention labels are labeled with underscore '_'), ignore builtin '__name' field
        state = ModelEndpointState(
            endpoint=ModelEndpoint(
                metadata=ModelEndpointMetadata(
                    project=endpoint.get("project"),
                    tag=endpoint.get("tag"),
                    labels=json.loads(endpoint.get("labels")),
                ),
                spec=ModelEndpointSpec(
                    model=endpoint.get("model"),
                    function=endpoint.get("function"),
                    model_class=endpoint.get("model_class"),
                ),
                status=ObjectStatus(state="active"),
            ),
            first_request=endpoint.get("first_request"),
            last_request=endpoint.get("last_request"),
            error_count=endpoint.get("error_count"),
            alert_count=endpoint.get("alert_count"),
            drift_status=endpoint.get("drift_status"),
            metrics=endpoint_metrics,
        )
        endpoint_state_list.append(state)

    return ModelEndpointStateList(endpoints=endpoint_state_list)


@router.get(
    "/projects/{project}/model-endpoints/{endpoint_id}",
    response_model=ModelEndpointState,
)
def get_endpoint(
    request: Request,
    project: str,
    endpoint_id: str,
    start: str = Query(default="now-1h"),
    end: str = Query(default="now"),
    metrics: bool = Query(default=False),
    features: bool = Query(default=False),
):
    """
    Return the current state of an endpoint, meaning all additional data the is relevant to a specified endpoint.
    This function also takes into account the start and end times and uses the same time-querying as v3io-frames.
    """

    _verify_endpoint(project, endpoint_id)
    secrets = get_model_endpoint_secrets(request)

    endpoint = get_endpoint_kv_record_by_id(
        secrets, project, endpoint_id, ENDPOINT_TABLE_ATTRIBUTES_WITH_FEATURES,
    )

    if not endpoint:
        url = f"/projects/{project}/model-endpoints/{endpoint_id}"
        raise MLRunNotFoundError(f"Endpoint {endpoint_id} not found - {url}")

    endpoint_metrics = None
    if metrics:
        endpoint_metrics = _get_endpoint_metrics(
            secrets=secrets,
            project=project,
            endpoint_id=endpoint_id,
            start=start,
            end=end,
            name=["predictions", "latency"],
        )

    endpoint_features = None
    if features:
        endpoint_features = _get_endpoint_features(
            project=project, endpoint_id=endpoint_id, features=endpoint.get("features")
        )

    return ModelEndpointState(
        endpoint=ModelEndpoint(
            metadata=ModelEndpointMetadata(
                project=endpoint.get("project"),
                tag=endpoint.get("tag"),
                labels=json.loads(endpoint.get("labels", "")),
            ),
            spec=ModelEndpointSpec(
                model=endpoint.get("model"),
                function=endpoint.get("function"),
                model_class=endpoint.get("model_class"),
            ),
            status=ObjectStatus(state="active"),
        ),
        first_request=endpoint.get("first_request"),
        last_request=endpoint.get("last_request"),
        error_count=endpoint.get("error_count"),
        alert_count=endpoint.get("alert_count"),
        drift_status=endpoint.get("drift_status"),
        metrics=endpoint_metrics,
        features=endpoint_features,
    )


def _get_endpoint_metrics(
    secrets: dict,
    project: str,
    endpoint_id: str,
    name: List[str],
    start: str = "now-1h",
    end: str = "now",
) -> List[Metric]:

    if not name:
        raise MLRunInvalidArgumentError("Metric names must be provided")

    try:
        metrics = [TimeMetric.from_string(n) for n in name]
    except NotImplementedError as e:
        raise MLRunInvalidArgumentError(str(e))

    # Columns must have at least an endpoint_id attribute for frames' filter expression
    columns = ["endpoint_id"]

    for metric in metrics:
        columns.append(metric.tsdb_column)

    client = get_frames_client(
        address=secrets[V3IO_FRAMESD],
        container=config.httpdb.model_endpoint_monitoring.container,
    )

    data = client.read(
        backend="tsdb",
        table=f"{project}/{ENDPOINT_EVENTS_TABLE_PATH}",
        columns=columns,
        filter=f"endpoint_id=='{endpoint_id}'",
        start=start,
        end=end,
    )

    metrics = [time_metric.transform_df_to_metric(data) for time_metric in metrics]
    metrics = [metric for metric in metrics if metric is not None]
    return metrics


def _get_endpoint_features(
    project: str, endpoint_id: str, features: Optional[str]
) -> List[Features]:
    if not features:
        url = f"projects/{project}/model-endpoints/{endpoint_id}/features"
        raise MLRunNotFoundError(f"Endpoint features not found' - {url}")

    parsed_features: List[dict] = json.loads(features) if features is not None else []
    feature_objects = [Features(**feature) for feature in parsed_features]
    return feature_objects


def _decode_labels(labels: Dict[str, Any]) -> List[str]:
    if not labels:
        return []
    return [f"{lbl.lstrip('_')}=={val}" for lbl, val in labels.items()]


def _build_kv_cursor_filter_expression(
    project: str,
    function: Optional[str],
    model: Optional[str],
    tag: Optional[str],
    labels: Optional[List[str]],
):
    filter_expression = [f"project=='{project}'"]

    if function:
        filter_expression.append(f"function=='{function}'")
    if model:
        filter_expression.append(f"model=='{model}'")
    if tag:
        filter_expression.append(f"tag=='{tag}'")
    if labels:
        for label in labels:

            if not label.startswith("_"):
                label = f"_{label}"

            if "=" in label:
                lbl, value = list(map(lambda x: x.strip(), label.split("=")))
                filter_expression.append(f"{lbl}=='{value}'")
            else:
                filter_expression.append(f"exists({label})")

    return " AND ".join(filter_expression)


def get_endpoint_kv_record_by_id(
    secrets: dict,
    project: str,
    endpoint_id: str,
    attribute_names: Optional[List[str]] = None,
) -> Dict[str, Any]:

    client = get_v3io_client(
        endpoint=secrets[V3IO_API], access_key=secrets[V3IO_ACCESS_KEY]
    )

    endpoint = client.kv.get(
        container=config.httpdb.model_endpoint_monitoring.container,
        table_path=f"{project}/{ENDPOINTS_TABLE_PATH}",
        key=endpoint_id,
        attribute_names=attribute_names or "*",
        raise_for_status=RaiseForStatus.never,
    ).output.item
    return endpoint


def _verify_endpoint(project, endpoint_id):
    endpoint_id_project, _ = endpoint_id.split(".")
    if endpoint_id_project != project:
        raise MLRunConflictError(
            f"project: {project} and endpoint_id: {endpoint_id} missmatch."
        )


def get_model_endpoint_secrets(_request: Request):
    access_key = _request.headers.get("X-V3io-Session-Key")
    if not access_key:
        raise MLRunBadRequestError(
            "Request header missing 'X-V3io-Session-Key' parameter."
        )

    v3io_api = environ.get("V3IO_WEBAPI_PORT_8081_TCP")
    if not v3io_api:
        raise MLRunPreconditionFailedError(
            "Environment missing 'V3IO_WEBAPI_PORT_8081_TCP' parameter."
        )
    else:
        v3io_api = v3io_api.replace("tcp", "http")

    v3io_framesd = environ.get("FRAMESD_PORT_8081_TCP")
    if not v3io_framesd:
        raise MLRunPreconditionFailedError(
            "Environment missing 'V3IO_WEBAPI_PORT_8081_TCP' parameter."
        )
    else:
        v3io_framesd = v3io_framesd.replace("tcp", "http")

    return {
        V3IO_ACCESS_KEY: access_key,
        V3IO_API: v3io_api,
        V3IO_FRAMESD: v3io_framesd,
    }
