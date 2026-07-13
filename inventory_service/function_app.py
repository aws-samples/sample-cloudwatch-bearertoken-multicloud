# inventory_service/function_app.py — Inventory Service (Azure Functions)
import time
import json
import os
import random
import logging
import azure.functions as func
from opentelemetry import metrics

from otel_setup import configure_otel, flush_telemetry

# Configure OTel at module level (runs once per cold start)
meter_provider, logger_provider = configure_otel(
    service_name="inventory-service",
    cloud_provider="azure",
    cloud_region="eastus",
    cloud_platform="azure_functions",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inventory-service")

# Custom metrics
meter = metrics.get_meter("inventory-service")
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
stock_level_gauge = meter.create_up_down_counter(
    "inventory_service.stock_level",
    description="Current stock level for queried products",
)
items_checked = meter.create_counter(
    "inventory_service.items_checked",
    description="Number of inventory items checked",
)
low_stock_alerts = meter.create_counter(
    "inventory_service.low_stock_alerts",
    description="Products found with critically low stock",
)
warehouse_lookups = meter.create_counter(
    "inventory_service.warehouse_lookups",
    description="Lookups by warehouse region",
)
db_query_duration = meter.create_histogram(
    "inventory_service.db_query.duration",
    unit="ms",
    description="Simulated database query latency",
)

# Common labels attached to every metric data point so PromQL can group/filter
# by service (resource attributes are not exposed as metric labels in CloudWatch).
SVC_LABELS = {"service_name": "inventory-service", "app_name": "ecommerce"}

FAKE_FULFILLMENT = [
    {"phone": "(206) 555-8679", "address": "123 Main St, Seattle, WA 98101"},
    {"phone": "(503) 555-2345", "address": "456 Oak Ave, Portland, OR 97201"},
    {"phone": "(415) 555-9012", "address": "789 Pine Rd, San Francisco, CA 94102"},
    {"phone": "(512) 555-4567", "address": "321 Elm Blvd, Austin, TX 73301"},
    {"phone": "(720) 555-6781", "address": "654 Maple Dr, Denver, CO 80201"},
]

# Shared API key for request validation
EXPECTED_API_KEY = os.environ.get("CROSS_CLOUD_API_KEY", "")

# Simulated inventory database
INVENTORY_DB = {
    str(i): {"stock": random.randint(0, 500), "warehouse": random.choice(["east", "west", "central"])}
    for i in range(1, 101)
}

app = func.FunctionApp()


@app.route(route="inventory/{product_id}", auth_level=func.AuthLevel.ANONYMOUS)
def inventory_lookup(req: func.HttpRequest) -> func.HttpResponse:
    """Handle inventory lookup requests."""
    start = time.time()

    # Validate API key
    api_key = req.headers.get("X-API-Key", "")
    if api_key != EXPECTED_API_KEY:
        logger.warning("Unauthorized request — invalid API key")
        error_counter.add(1, {**SVC_LABELS, "error_type": "unauthorized"})
        return func.HttpResponse(
            json.dumps({"error": "unauthorized"}),
            status_code=401,
            mimetype="application/json",
        )

    product_id = req.route_params.get("product_id", "0")
    contact = random.choice(FAKE_FULFILLMENT)
    logger.info(f"Inventory lookup for product_id={product_id} "
                f"fulfillment_contact={contact['phone']} "
                f"shipping_address={contact['address']}")

    # Spike simulation: when X-Simulate-Spike header targets this service,
    # force high error rate and latency to reproduce the blog troubleshooting scenario
    simulate_spike = req.headers.get("X-Simulate-Spike", "")
    if simulate_spike in ("inventory", "all"):
        time.sleep(random.uniform(0.8, 2.0))
        if random.random() < 0.85:
            error_type = random.choice(["database_error", "connection_timeout", "lock_contention"])
            logger.error(f"{error_type} for product_id={product_id}")
            error_counter.add(1, {**SVC_LABELS, "error_type": error_type, "product_id": product_id})
            latency_ms = (time.time() - start) * 1000
            latency_histogram.record(latency_ms, {**SVC_LABELS, "status": "error"})
            db_query_duration.record(random.uniform(800, 3000), {"table": "inventory", "operation": "select"})
            flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)
            return func.HttpResponse(
                json.dumps({"error": "database_connection_error"}),
                status_code=503,
                mimetype="application/json",
            )

    # Simulate occasional processing delay
    if random.random() < 0.15:
        time.sleep(random.uniform(0.2, 0.8))

    # Simulate random errors at higher rate for demo
    if random.random() < 0.12:
        error_type = random.choice(["database_error", "connection_timeout", "lock_contention", "cache_miss_cascade"])
        logger.error(f"{error_type} for product_id={product_id}")
        error_counter.add(1, {**SVC_LABELS, "error_type": error_type, "product_id": product_id})
        latency_ms = (time.time() - start) * 1000
        latency_histogram.record(latency_ms, {**SVC_LABELS, "status": "error"})
        flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)
        return func.HttpResponse(
            json.dumps({"error": "database_connection_error"}),
            status_code=503,
            mimetype="application/json",
        )

    # Look up inventory
    db_start = time.time()
    inventory = INVENTORY_DB.get(product_id, {"stock": 0, "warehouse": "unknown"})
    # Simulate variable DB latency
    db_sim_ms = random.uniform(0.5, 15.0)
    db_query_duration.record(db_sim_ms, {"table": "inventory", "operation": "select"})

    result = {
        "product_id": product_id,
        "stock": inventory["stock"],
        "warehouse": inventory["warehouse"],
        "in_stock": inventory["stock"] > 0,
    }

    # Record metrics — always produces values
    latency_ms = (time.time() - start) * 1000
    latency_histogram.record(latency_ms, {**SVC_LABELS, "status": "success"})
    request_counter.add(1, {**SVC_LABELS, "status": "success", "product_id_range": f"{int(product_id)//10*10}-{int(product_id)//10*10+9}"})
    stock_level_gauge.add(inventory["stock"], {"product_id": product_id, "warehouse": inventory["warehouse"]})
    items_checked.add(1, {"warehouse": inventory["warehouse"], "in_stock": str(inventory["stock"] > 0).lower()})
    warehouse_lookups.add(1, {"warehouse": inventory["warehouse"]})

    if inventory["stock"] < 20:
        low_stock_alerts.add(1, {"warehouse": inventory["warehouse"], "severity": "critical" if inventory["stock"] < 5 else "warning"})

    logger.info(f"Inventory result product_id={product_id} stock={inventory['stock']} "
                f"latency_ms={latency_ms:.1f}")

    # Flush telemetry before function returns
    flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)

    return func.HttpResponse(
        json.dumps(result),
        status_code=200,
        mimetype="application/json",
    )
