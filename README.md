# Serverless Multi-Cloud Observability — Sample Code

Reference implementation for the blog post: **"Achieve Serverless Multi-Cloud Observability with Amazon CloudWatch and OpenTelemetry"**

## Structure

```
serverless-multicloud-code/
├── shared/
│   └── otel_setup.py              # Shared OTel config (copy into each function)
├── product_api/                   # AWS Lambda (Python 3.12)
│   ├── handler.py                 # Lambda handler
│   ├── otel_setup.py              # ← copy from shared/
│   ├── requirements.txt
│   └── template.yaml              # SAM template
├── inventory_service/             # Azure Functions (Python 3.11)
│   ├── function_app.py            # Azure Function handler
│   ├── otel_setup.py              # ← copy from shared/
│   └── requirements.txt
├── pricing_service/               # GCP Cloud Functions 2nd gen (Python 3.12)
│   ├── main.py                    # Cloud Function handler
│   ├── otel_setup.py              # ← copy from shared/
│   └── requirements.txt
├── .env.example                   # Environment variables template
└── README.md
```

## Setup

1. Copy `shared/otel_setup.py` into each function directory before deploying
2. Copy `.env.example` to `.env` and fill in your values
3. Generate two separate CloudWatch OTLP bearer tokens:
   - One for the **metrics** endpoint (`CW_METRICS_BEARER_TOKEN`)
   - One for the **logs** endpoint (`CW_LOGS_BEARER_TOKEN`)

## Key Points

- **No collector/agent** — OTel SDK exports directly to CloudWatch OTLP endpoints
- **Separate bearer tokens** — metrics and logs endpoints use different tokens
- **Force-flush** — each handler calls `flush_telemetry()` before returning
- **Module-level init** — OTel SDK configured once per cold start, reused across warm invocations

## Local Testing

```bash
# Install deps
cd product_api && pip install -r requirements.txt

# Set env vars
export CW_METRICS_BEARER_TOKEN="..."
export CW_LOGS_BEARER_TOKEN="..."
export CW_REGION="us-east-1"
export CW_LOG_GROUP="/multicloud/ecommerce/product-api"
export CW_LOG_STREAM="local-test"

# Test Lambda locally with SAM
sam local invoke ProductApiFunction --event events/get_product.json
```

## Deployment

### AWS Lambda (Product API)

The Product API is packaged as an AWS SAM application (`product_api/template.yaml`).

**Prerequisites:**
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed
- AWS credentials configured (`aws configure` or a named profile)
- Python 3.12 available for the build

**Steps:**

```bash
cd product_api

# 1. Copy the shared OTel config into the function directory
cp ../shared/otel_setup.py .

# 2. Build the function (installs dependencies from requirements.txt)
sam build

# 3. Deploy (guided the first time — prompts for stack name, region, and parameters)
sam deploy --guided \
  --parameter-overrides \
    "InventoryServiceUrl=<your-azure-function-url>" \
    "PricingServiceUrl=<your-gcp-function-url>" \
    "CrossCloudApiKey=<your-shared-api-key>"
```

The stack provisions the Lambda function, an API Gateway REST API, and the IAM role.
On success, SAM prints the `ProductApiUrl` output — the endpoint for `GET /products/{product_id}`.

Subsequent deploys can drop `--guided` (settings are saved to `samconfig.toml`):

```bash
sam build && sam deploy
```

> **Note:** The template reads the CloudWatch OTLP bearer tokens from AWS Secrets Manager
> (`CW_METRICS_SECRET_NAME` / `CW_LOGS_SECRET_NAME`). Create those secrets in the same
> region before deploying, or switch to the `CW_METRICS_BEARER_TOKEN` / `CW_LOGS_BEARER_TOKEN`
> environment variables (see `.env.example`).

### Azure Functions (Inventory Service)

Deploy `inventory_service/` following the official Azure documentation:

- [Deploy Python Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)
- [Deploy with the Azure Functions Core Tools (`func azure functionapp publish`)](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- [Deploy with the Azure CLI (`az functionapp`)](https://learn.microsoft.com/en-us/cli/azure/functionapp)

Set the CloudWatch OTLP tokens and config (`CW_METRICS_BEARER_TOKEN`, `CW_LOGS_BEARER_TOKEN`,
`CW_REGION`, `CW_LOG_GROUP`, `CW_LOG_STREAM`) as application settings on the Function App.

### GCP Cloud Functions (Pricing Service)

Deploy `pricing_service/` following the official Google Cloud documentation:

- [Deploy Cloud Functions (2nd gen)](https://cloud.google.com/functions/docs/deploy)
- [`gcloud functions deploy` reference](https://cloud.google.com/sdk/gcloud/reference/functions/deploy)

Set the CloudWatch OTLP tokens and config (`CW_METRICS_BEARER_TOKEN`, `CW_LOGS_BEARER_TOKEN`,
`CW_REGION`, `CW_LOG_GROUP`, `CW_LOG_STREAM`) as runtime environment variables via
`--set-env-vars` or `--env-vars-file`.
