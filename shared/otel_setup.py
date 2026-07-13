# otel_setup.py — Shared OpenTelemetry configuration for direct CloudWatch OTLP export
import os
import json
import logging
try:
    import boto3
    from botocore.exceptions import ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

logger = logging.getLogger(__name__)

# Configuration from environment variables
CW_REGION = os.environ.get("CW_REGION", "us-east-1")
CW_METRICS_BEARER_TOKEN = os.environ.get("CW_METRICS_BEARER_TOKEN", "")
CW_LOG_GROUP = os.environ.get("CW_LOG_GROUP", "/multicloud/ecommerce")
CW_LOG_STREAM = os.environ.get("CW_LOG_STREAM", "default")

# Secrets Manager configuration for bearer tokens
CW_LOGS_SECRET_NAME = os.environ.get("CW_LOGS_SECRET_NAME", "")  # e.g., "LogsAPIKey-oenx-at-111122223333"
CW_METRICS_SECRET_NAME = os.environ.get("CW_METRICS_SECRET_NAME", "")  # e.g., "MetricsAPIKey-at-111122223333"

METRICS_ENDPOINT = f"https://monitoring.{CW_REGION}.amazonaws.com/v1/metrics"
LOGS_ENDPOINT = f"https://logs.{CW_REGION}.amazonaws.com/v1/logs"

# Cache for secrets (avoid repeated API calls across warm invocations)
_secrets_cache = {}


def get_secret(secret_name: str, region: str = None) -> str:
    """
    Retrieve a secret value from AWS Secrets Manager.
    Results are cached for the lifetime of the execution environment (warm starts).
    """
    if not HAS_BOTO3:
        raise RuntimeError("boto3 not available — use CW_LOGS_BEARER_TOKEN env var instead")

    if secret_name in _secrets_cache:
        return _secrets_cache[secret_name]

    region = region or CW_REGION
    client = boto3.client("secretsmanager", region_name=region)

    try:
        response = client.get_secret_value(SecretId=secret_name)
        # Handle both string and binary secrets
        if "SecretString" in response:
            secret_value = response["SecretString"]
            # If it's JSON, try to extract a "token" or "key" field
            try:
                parsed = json.loads(secret_value)
                # Support common key names
                for key in ("apiKeySecret", "token", "key", "bearer_token", "api_key", "value"):
                    if key in parsed:
                        secret_value = parsed[key]
                        break
            except (json.JSONDecodeError, TypeError):
                pass  # Plain string secret — use as-is
        else:
            secret_value = response["SecretBinary"].decode("utf-8")

        _secrets_cache[secret_name] = secret_value
        logger.info(f"Successfully retrieved secret: {secret_name}")
        return secret_value

    except ClientError as e:
        logger.error(f"Failed to retrieve secret '{secret_name}': {e}")
        raise


def get_logs_bearer_token() -> str:
    """
    Get the logs endpoint bearer token.
    Priority: Secrets Manager (if CW_LOGS_SECRET_NAME is set) > environment variable.
    """
    if CW_LOGS_SECRET_NAME:
        return get_secret(CW_LOGS_SECRET_NAME)
    return os.environ.get("CW_LOGS_BEARER_TOKEN", "")


def get_metrics_bearer_token() -> str:
    """
    Get the metrics endpoint bearer token.
    Priority: Secrets Manager (if CW_METRICS_SECRET_NAME is set) > environment variable.
    """
    if CW_METRICS_SECRET_NAME:
        return get_secret(CW_METRICS_SECRET_NAME)
    return os.environ.get("CW_METRICS_BEARER_TOKEN", "")


def configure_otel(service_name: str, cloud_provider: str, cloud_region: str,
                   cloud_platform: str) -> tuple:
    """
    Configure OpenTelemetry SDK for direct OTLP export to CloudWatch.
    
    Returns:
        tuple: (MeterProvider, LoggerProvider) for cleanup/flush on function exit
    """
    # Define resource attributes for cross-cloud correlation
    resource = Resource.create({
        "service.name": service_name,
        "app.name": "ecommerce",
        "service.namespace": "MultiCloud/ECommerce",
        "cloud.provider": cloud_provider,
        "cloud.region": cloud_region,
        "cloud.platform": cloud_platform,
        "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "production"),
    })

    # --- Metrics: Direct OTLP export to CloudWatch ---
    metrics_bearer_token = get_metrics_bearer_token()

    metric_exporter = OTLPMetricExporter(
        endpoint=METRICS_ENDPOINT,
        headers={
            "Authorization": f"Bearer {metrics_bearer_token}",
        },
        timeout=10,
    )
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=5000,
        export_timeout_millis=5000,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
    )
    metrics.set_meter_provider(meter_provider)

    # --- Logs: Direct OTLP export to CloudWatch ---
    # Retrieve logs bearer token (from Secrets Manager on AWS, env var elsewhere)
    logs_bearer_token = get_logs_bearer_token()

    log_exporter = OTLPLogExporter(
        endpoint=LOGS_ENDPOINT,
        headers={
            "Authorization": f"Bearer {logs_bearer_token}",
            "x-aws-log-group": CW_LOG_GROUP,
            "x-aws-log-stream": CW_LOG_STREAM,
        },
        timeout=10,
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            log_exporter,
            max_export_batch_size=64,
            schedule_delay_millis=2000,  # Flush logs every 2 seconds
            max_queue_size=512,
            export_timeout_millis=5000,
        )
    )
    set_logger_provider(logger_provider)

    # Attach OTel logging handler to application loggers only (not the root logger,
    # which causes recursion when OTel internals emit warnings via logging)
    otel_handler = LoggingHandler(
        level=logging.INFO,
        logger_provider=logger_provider,
    )
    for name in ("product-api", "inventory-service", "pricing-service"):
        svc_logger = logging.getLogger(name)
        svc_logger.addHandler(otel_handler)
        svc_logger.setLevel(logging.INFO)

    logger.info(f"OTel configured for {service_name} — direct export to CloudWatch "
                f"({CW_REGION})")

    return meter_provider, logger_provider


def flush_telemetry(meter_provider, logger_provider, timeout_millis: int = 5000):
    """
    Force-flush all pending metrics and logs before the function returns.
    Critical for serverless — the execution environment may freeze immediately
    after the handler returns.
    """
    try:
        meter_provider.force_flush(timeout_millis=timeout_millis)
    except Exception as e:
        logger.warning(f"Metrics flush failed: {e}")

    try:
        logger_provider.force_flush(timeout_millis=timeout_millis)
    except Exception as e:
        logger.warning(f"Logs flush failed: {e}")
