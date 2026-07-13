# product_api/handler.py — Product API (AWS Lambda)
import time
import json
import os
import logging
import requests
from opentelemetry import metrics

from otel_setup import configure_otel, flush_telemetry

# Configure OTel at module level (runs once per cold start)
meter_provider, logger_provider = configure_otel(
    service_name="product-api",
    cloud_provider="aws",
    cloud_region=os.environ.get("AWS_REGION", "us-east-1"),
    cloud_platform="aws_lambda",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("product-api")

# Custom metrics
meter = metrics.get_meter("product-api")
request_counter = meter.create_counter(
    "requests.total",
    description="Total requests processed",
)
latency_histogram = meter.create_histogram(
    "request.duration",
    unit="ms",
    description="Request latency in milliseconds",
)
error_counter = meter.create_counter(
    "errors.total",
    description="Total errors in processing",
)
downstream_error_counter = meter.create_counter(
    "product_api.downstream_errors.total",
    description="Errors calling downstream services",
)
downstream_latency_histogram = meter.create_histogram(
    "product_api.downstream.duration",
    unit="ms",
    description="Latency calling each downstream service",
)
response_size = meter.create_histogram(
    "product_api.response.size",
    unit="bytes",
    description="Response payload size in bytes",
)
active_products = meter.create_up_down_counter(
    "product_api.active_products",
    description="Products queried in this window",
)
cold_start_counter = meter.create_counter(
    "product_api.cold_starts",
    description="Number of cold starts detected",
)

import random as _random
_is_cold_start = True

# Common labels attached to every metric data point so PromQL can group/filter
# by service (resource attributes are not exposed as metric labels in CloudWatch).
SVC_LABELS = {"service_name": "product-api", "app_name": "ecommerce"}

FAKE_CUSTOMERS = [
    {"email": "john.doe@example.com", "ip": "203.0.113.42", "phone": "+1-555-867-5309"},
    {"email": "jane.smith@company.org", "ip": "198.51.100.17", "phone": "+1-555-234-5678"},
    {"email": "bob.wilson@gmail.com", "ip": "192.0.2.88", "phone": "+1-555-901-2345"},
    {"email": "alice.jones@yahoo.com", "ip": "203.0.113.99", "phone": "+1-555-456-7890"},
    {"email": "carlos.garcia@hotmail.com", "ip": "198.51.100.200", "phone": "+1-555-678-1234"},
]

# Downstream service configuration
INVENTORY_SERVICE_URL = os.environ.get("INVENTORY_SERVICE_URL", "")
PRICING_SERVICE_URL = os.environ.get("PRICING_SERVICE_URL", "")
CROSS_CLOUD_API_KEY = os.environ.get("CROSS_CLOUD_API_KEY", "")


def call_downstream_service(url: str, service_name: str, cloud: str,
                            simulate_spike: str = "") -> dict:
    """Call a downstream service with error handling and metrics."""
    ds_start = time.time()
    headers = {"X-API-Key": CROSS_CLOUD_API_KEY}
    if simulate_spike:
        headers["X-Simulate-Spike"] = simulate_spike
    try:
        response = requests.get(
            url,
            timeout=10,
            headers=headers,
        )
        ds_latency = (time.time() - ds_start) * 1000
        downstream_latency_histogram.record(ds_latency, {
            "target_service": service_name,
            "cloud": cloud,
            "status": "success" if response.ok else "error",
        })
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        ds_latency = (time.time() - ds_start) * 1000
        downstream_latency_histogram.record(ds_latency, {
            "target_service": service_name, "cloud": cloud, "status": "timeout",
        })
        logger.error(f"{service_name} call timed out — url={url}")
        downstream_error_counter.add(1, {
            "target_service": service_name,
            "cloud": cloud,
            "error_type": "timeout",
        })
        return {"error": "timeout", "service": service_name}
    except requests.exceptions.RequestException as e:
        ds_latency = (time.time() - ds_start) * 1000
        downstream_latency_histogram.record(ds_latency, {
            "target_service": service_name, "cloud": cloud, "status": "error",
        })
        logger.error(f"{service_name} call failed: {e}")
        downstream_error_counter.add(1, {
            "target_service": service_name,
            "cloud": cloud,
            "error_type": "request_error",
        })
        return {"error": str(e), "service": service_name}


def lambda_handler(event, context):
    """AWS Lambda handler for Product API."""
    start = time.time()

    # Extract product ID from API Gateway path parameters
    path_params = event.get("pathParameters", {}) or {}
    product_id = path_params.get("product_id", "0")

    # Simulate random errors for demo/testing
    import random
    if random.random() < 0.08:
        logger.error(f"Internal processing error for product_id={product_id} "
                     f"request_id={context.aws_request_id}")
        error_counter.add(1, {**SVC_LABELS, "endpoint": "/products/{id}", "error_type": "internal"})
        latency_ms = (time.time() - start) * 1000
        latency_histogram.record(latency_ms, {**SVC_LABELS, "endpoint": "/products/{id}", "status": "error"})
        request_counter.add(1, {**SVC_LABELS, "endpoint": "/products/{id}", "status": "error"})
        flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "internal_processing_error"}),
        }

    if random.random() < 0.05:
        time.sleep(random.uniform(2.0, 5.0))
        logger.warning(f"Slow processing detected for product_id={product_id} "
                       f"request_id={context.aws_request_id}")

    customer = _random.choice(FAKE_CUSTOMERS)
    logger.info(f"Processing request for product_id={product_id} "
                f"request_id={context.aws_request_id} "
                f"customer_email={customer['email']} client_ip={customer['ip']}")

    # Check for spike simulation header (forwarded to downstream services)
    headers = event.get("headers", {}) or {}
    simulate_spike = headers.get("X-Simulate-Spike", headers.get("x-simulate-spike", ""))

    # Call Inventory Service (Azure Functions)
    inventory = call_downstream_service(
        url=f"{INVENTORY_SERVICE_URL}/api/inventory/{product_id}",
        service_name="inventory-service",
        cloud="azure",
        simulate_spike=simulate_spike,
    )

    # Call Pricing Service (GCP Cloud Functions)
    pricing = call_downstream_service(
        url=f"{PRICING_SERVICE_URL}/pricing/{product_id}",
        service_name="pricing-service",
        cloud="gcp",
        simulate_spike=simulate_spike,
    )

    # Assemble response
    result = {
        "id": int(product_id),
        "name": f"Product-{product_id}",
        "inventory": inventory,
        "pricing": pricing,
    }

    # Record metrics
    global _is_cold_start
    latency_ms = (time.time() - start) * 1000
    has_error = "error" in inventory or "error" in pricing
    status = "partial_error" if has_error else "success"
    region = os.environ.get("AWS_REGION", "us-east-1")

    latency_histogram.record(latency_ms, {**SVC_LABELS, "endpoint": "/products/{id}", "status": status, "region": region})
    request_counter.add(1, {**SVC_LABELS, "endpoint": "/products/{id}", "status": status, "region": region})
    active_products.add(1, {"product_id_range": f"{int(product_id)//10*10}-{int(product_id)//10*10+9}"})

    body = json.dumps(result)
    response_size.record(len(body), {"status": status, "content_type": "application/json"})

    if _is_cold_start:
        cold_start_counter.add(1, {"region": region})
        _is_cold_start = False

    if has_error:
        error_counter.add(1, {**SVC_LABELS, "endpoint": "/products/{id}", "error_type": "downstream"})

    logger.info(f"Completed product_id={product_id} status={status} "
                f"latency_ms={latency_ms:.1f}")

    # CRITICAL: Flush telemetry before Lambda freezes the execution environment
    flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result),
    }
