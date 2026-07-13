# pricing_service/main.py — Pricing Service (GCP Cloud Functions 2nd gen)
import time
import json
import os
import random
import logging
import functions_framework
from opentelemetry import metrics

from otel_setup import configure_otel, flush_telemetry

# Configure OTel at module level (runs once per cold start)
meter_provider, logger_provider = configure_otel(
    service_name="pricing-service",
    cloud_provider="gcp",
    cloud_region="us-central1",
    cloud_platform="gcp_cloud_functions",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pricing-service")

# Custom metrics
meter = metrics.get_meter("pricing-service")
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
price_calculated = meter.create_histogram(
    "pricing_service.price_calculated",
    unit="USD",
    description="Distribution of calculated prices",
)
discount_amount = meter.create_histogram(
    "pricing_service.discount_amount",
    unit="USD",
    description="Dollar amount of discount applied",
)
revenue_estimate = meter.create_counter(
    "pricing_service.revenue_estimate",
    description="Estimated revenue from price calculations",
)
cache_hits = meter.create_counter(
    "pricing_service.cache",
    description="Price cache hit/miss counter",
)
pricing_rules_evaluated = meter.create_counter(
    "pricing_service.rules_evaluated",
    description="Number of pricing rules evaluated",
)

# Common labels attached to every metric data point so PromQL can group/filter
# by service (resource attributes are not exposed as metric labels in CloudWatch).
SVC_LABELS = {"service_name": "pricing-service", "app_name": "ecommerce"}

FAKE_PAYMENTS = [
    {"card": "4111-1111-1111-1234", "email": "john.doe@example.com"},
    {"card": "5500-0000-0000-5678", "email": "jane.smith@company.org"},
    {"card": "3400-000000-09012", "email": "bob.wilson@gmail.com"},
    {"card": "6011-0000-0000-3456", "email": "alice.jones@yahoo.com"},
    {"card": "4222-2222-2222-7890", "email": "carlos.garcia@hotmail.com"},
]

# Shared API key for request validation
EXPECTED_API_KEY = os.environ.get("CROSS_CLOUD_API_KEY", "")

# Simulated pricing rules
BASE_PRICES = {str(i): round(random.uniform(9.99, 299.99), 2) for i in range(1, 101)}
DISCOUNT_TIERS = {"standard": 0.0, "premium": 0.10, "enterprise": 0.20}


@functions_framework.http
def pricing_lookup(request):
    """Handle pricing lookup requests."""
    start = time.time()

    # Validate API key
    api_key = request.headers.get("X-API-Key", "")
    if api_key != EXPECTED_API_KEY:
        logger.warning("Unauthorized request — invalid API key")
        error_counter.add(1, {**SVC_LABELS, "error_type": "unauthorized"})
        return json.dumps({"error": "unauthorized"}), 401, {"Content-Type": "application/json"}

    # Extract product_id from path: /pricing/<product_id>
    path = request.path
    parts = path.strip("/").split("/")
    product_id = parts[-1] if len(parts) >= 1 else "0"

    payment = random.choice(FAKE_PAYMENTS)
    logger.info(f"Pricing lookup for product_id={product_id} "
                f"payment_card={payment['card']} customer_email={payment['email']}")

    # Simulate occasional processing delay
    if random.random() < 0.15:
        time.sleep(random.uniform(0.1, 0.6))

    # Simulate random errors at higher rate for demo
    if random.random() < 0.10:
        error_type = random.choice(["pricing_engine_error", "stale_cache", "rate_limit_exceeded", "model_timeout"])
        logger.error(f"{error_type} for product_id={product_id}")
        error_counter.add(1, {**SVC_LABELS, "error_type": error_type, "product_id": product_id})
        latency_ms = (time.time() - start) * 1000
        latency_histogram.record(latency_ms, {**SVC_LABELS, "status": "error"})
        flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)
        return json.dumps({"error": "pricing_engine_error"}), 503, {"Content-Type": "application/json"}

    # Calculate price
    base_price = BASE_PRICES.get(product_id, 49.99)
    discount_tier = random.choice(list(DISCOUNT_TIERS.keys()))
    discount = DISCOUNT_TIERS[discount_tier]
    final_price = round(base_price * (1 - discount), 2)

    result = {
        "product_id": product_id,
        "base_price": base_price,
        "discount_tier": discount_tier,
        "discount_percent": int(discount * 100),
        "final_price": final_price,
        "currency": "USD",
    }

    # Record metrics — always produces rich values
    latency_ms = (time.time() - start) * 1000
    latency_histogram.record(latency_ms, {**SVC_LABELS, "status": "success"})
    request_counter.add(1, {**SVC_LABELS, "status": "success", "discount_tier": discount_tier})
    price_calculated.record(final_price, {"product_id": product_id, "discount_tier": discount_tier})

    discount_dollars = round(base_price - final_price, 2)
    discount_amount.record(discount_dollars, {"discount_tier": discount_tier})
    revenue_estimate.add(int(final_price * 100), {"discount_tier": discount_tier, "currency": "USD_cents"})

    is_cache_hit = random.random() < 0.7
    cache_hits.add(1, {"result": "hit" if is_cache_hit else "miss"})

    rules_count = random.randint(2, 8)
    pricing_rules_evaluated.add(rules_count, {"discount_tier": discount_tier})

    logger.info(f"Pricing result product_id={product_id} final_price={final_price} "
                f"tier={discount_tier} latency_ms={latency_ms:.1f}")

    # Flush telemetry before function returns
    flush_telemetry(meter_provider, logger_provider, timeout_millis=3000)

    return json.dumps(result), 200, {"Content-Type": "application/json"}
