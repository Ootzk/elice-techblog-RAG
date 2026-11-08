from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from elice_knowledge.settings import Settings


def client(settings: Settings) -> MlflowClient:
    if settings.mlflow_tracking_uri is None:
        raise ValueError("MLFLOW_TRACKING_URI is required when MLflow is enabled")
    return MlflowClient(tracking_uri=settings.mlflow_tracking_uri.get_secret_value())


def check_tracking(settings: Settings):
    client(settings).search_experiments(max_results=1)


def record_connectivity(settings: Settings, result: dict, profile: dict) -> str:
    tracking = client(settings)
    name = settings.mlflow_experiment_name
    experiment = tracking.get_experiment_by_name(name)
    if experiment is None:
        try:
            experiment_id = tracking.create_experiment(name)
        except MlflowException:
            # Another smoke check may have created the experiment concurrently.
            experiment = tracking.get_experiment_by_name(name)
            if experiment is None:
                raise
            experiment_id = experiment.experiment_id
    else:
        experiment_id = experiment.experiment_id
    run = tracking.create_run(
        experiment_id,
        tags={
            "mlflow.runName": "infrastructure-connectivity",
            "purpose": "connectivity-only",
            "app.version": "0.1.0",
            "app.env": settings.app_env,
        },
    )
    run_id = run.info.run_id
    try:
        for key in ("sample_id", "profile_id"):
            tracking.log_param(run_id, key, result[key])
        tracking.log_param(run_id, "embedding_model_digest", profile["digest"])
        tracking.log_metric(run_id, "embedding_dimensions", result["dimensions"])
        tracking.log_metric(run_id, "nearest_distance", result["nearest_distance"])
        tracking.log_metric(run_id, "connectivity_success", 1)
        tracking.set_terminated(run_id, "FINISHED")
    except Exception:
        tracking.set_terminated(run_id, "FAILED")
        raise
    return run_id
