#!/bin/bash
# Generate traffic to the Product API to produce telemetry data.
#
# Usage:
#   ./generate_traffic.sh [OPTIONS]
#
# Options:
#   --requests N     Total number of requests (default: 50)
#   --delay S        Seconds between requests (default: 1)
#   --spike          Simulate the "intermittent latency spike" incident from the blog.
#                    Sends normal traffic, then triggers a 2-minute inventory service
#                    degradation (85% errors + high latency), then resumes normal traffic.
#   --spike-duration S  Duration of the spike window in seconds (default: 120)
#   --url URL        API Gateway endpoint URL

set -euo pipefail

# Defaults
NUM_REQUESTS=50
DELAY=1
SPIKE_MODE=false
SPIKE_DURATION=120
# Override the API endpoint directly with API_URL, or set API_ID + REGION to build it.
REGION="${REGION:-us-west-2}"
API_ID="${API_ID:-your-api-id}"
API_URL="${API_URL:-https://${API_ID}.execute-api.${REGION}.amazonaws.com/Prod/products}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --requests)   NUM_REQUESTS="$2"; shift 2 ;;
        --delay)      DELAY="$2"; shift 2 ;;
        --spike)      SPIKE_MODE=true; shift ;;
        --spike-duration) SPIKE_DURATION="$2"; shift 2 ;;
        --url)        API_URL="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)            echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== Traffic Generator ==="
echo "Endpoint:  $API_URL"
echo "Requests:  $NUM_REQUESTS"
echo "Delay:     ${DELAY}s"
if $SPIKE_MODE; then
    echo "Mode:      SPIKE SIMULATION"
    echo "Pattern:   normal → ${SPIKE_DURATION}s spike (inventory) → normal"
    echo ""
    echo "The spike window sends X-Simulate-Spike: inventory to trigger"
    echo "high error rates and latency in the inventory service."
fi
echo ""

send_request() {
    local product_id=$1
    local spike_header="${2:-}"
    local curl_args=(-s -w "\n%{http_code} %{time_total}")

    if [[ -n "$spike_header" ]]; then
        curl_args+=(-H "X-Simulate-Spike: $spike_header")
    fi

    local response
    response=$(curl "${curl_args[@]}" "$API_URL/$product_id")
    local http_code
    http_code=$(echo "$response" | tail -1 | awk '{print $1}')
    local latency
    latency=$(echo "$response" | tail -1 | awk '{print $2}')
    echo "$http_code ($latency)"
}

if $SPIKE_MODE; then
    # Phase 1: Normal baseline traffic (1/3 of requests)
    BASELINE_COUNT=$((NUM_REQUESTS / 3))
    SPIKE_COUNT=$((NUM_REQUESTS / 3))
    RECOVERY_COUNT=$((NUM_REQUESTS - BASELINE_COUNT - SPIKE_COUNT))

    echo "--- Phase 1: Baseline ($BASELINE_COUNT requests) ---"
    for i in $(seq 1 $BASELINE_COUNT); do
        PRODUCT_ID=$((RANDOM % 100 + 1))
        echo -n "[$i/$NUM_REQUESTS] GET /products/$PRODUCT_ID ... "
        send_request "$PRODUCT_ID"
        sleep "$DELAY"
    done

    # Phase 2: Spike — inventory service degradation
    echo ""
    echo "--- Phase 2: SPIKE ACTIVE ($SPIKE_COUNT requests, inventory degraded) ---"
    echo "    Sending X-Simulate-Spike: inventory"
    echo ""
    SPIKE_START=$(date +%s)
    for i in $(seq 1 $SPIKE_COUNT); do
        GLOBAL_I=$((BASELINE_COUNT + i))
        PRODUCT_ID=$((RANDOM % 100 + 1))
        echo -n "[$GLOBAL_I/$NUM_REQUESTS] GET /products/$PRODUCT_ID [SPIKE] ... "
        send_request "$PRODUCT_ID" "inventory"
        sleep "$DELAY"
        # Check spike duration limit
        ELAPSED=$(( $(date +%s) - SPIKE_START ))
        if [[ $ELAPSED -ge $SPIKE_DURATION ]]; then
            echo "    Spike duration reached (${SPIKE_DURATION}s), moving to recovery."
            break
        fi
    done

    # Phase 3: Recovery — back to normal
    echo ""
    echo "--- Phase 3: Recovery ($RECOVERY_COUNT requests, normal) ---"
    for i in $(seq 1 $RECOVERY_COUNT); do
        GLOBAL_I=$((BASELINE_COUNT + SPIKE_COUNT + i))
        PRODUCT_ID=$((RANDOM % 100 + 1))
        echo -n "[$GLOBAL_I/$NUM_REQUESTS] GET /products/$PRODUCT_ID ... "
        send_request "$PRODUCT_ID"
        sleep "$DELAY"
    done

else
    # Standard mode: uniform traffic
    for i in $(seq 1 $NUM_REQUESTS); do
        PRODUCT_ID=$((RANDOM % 100 + 1))
        echo -n "[$i/$NUM_REQUESTS] GET /products/$PRODUCT_ID ... "
        send_request "$PRODUCT_ID"
        sleep "$DELAY"
    done
fi

echo ""
echo "Done. Check CloudWatch for telemetry data."
if $SPIKE_MODE; then
    echo ""
    echo "To reproduce the blog troubleshooting scenario, run these queries in CloudWatch:"
    echo "  Metrics Insights: sum(rate(errors.total[5m])) by (service.name, error_type)"
    echo "  Logs Insights:    filter service.name = \"inventory-service\" AND @message like /error|timeout|connection/"
fi
