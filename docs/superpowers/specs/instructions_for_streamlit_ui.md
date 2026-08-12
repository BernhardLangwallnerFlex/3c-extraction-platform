# Deploying Streamlit UI to Azure Container Apps

## Goal

Deploy the 3C VetCostCheck Streamlit testing UI as an Azure Container App in the same environment as the existing API. This gives the UI internal access to the API (no IP whitelisting needed) and allows embedding the API's Swagger docs via Swagger UI.

---

## Existing Azure Infrastructure

| Resource | Name | Details |
|----------|------|---------|
| Resource Group | `rg-3c-invoice` | Germany West Central |
| ACA Environment | `cae-3c-invoice` | Shared environment for all apps |
| Container Registry | `cr3cinvoice` (`cr3cinvoice.azurecr.io`) | Basic tier, admin enabled |
| API Container App | `ca-invoice-api` | FastAPI, port 8000, external ingress |
| API Public URL | `https://3cvetcostcheck.flex-capital-scale.com` | Custom domain with managed TLS |
| API Internal URL | `http://ca-invoice-api` | Only accessible from within `cae-3c-invoice` |

---

## Deployment Steps

### 1. Build and push the Docker image

From the Streamlit project root:

```bash
az acr build --registry cr3cinvoice --image vetcostcheck-ui:v1 .
```

The Dockerfile should expose the Streamlit port (default 8501):

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

### 2. Create the container app

```bash
ACR_SERVER="cr3cinvoice.azurecr.io"
ACR_PASS=$(az acr credential show --name cr3cinvoice --query "passwords[0].value" -o tsv)

az containerapp create \
  --name ca-vetcostcheck-ui \
  --resource-group rg-3c-invoice \
  --environment cae-3c-invoice \
  --image "${ACR_SERVER}/vetcostcheck-ui:v1" \
  --registry-server "$ACR_SERVER" \
  --registry-username cr3cinvoice \
  --registry-password "$ACR_PASS" \
  --target-port 8501 \
  --ingress external \
  --transport http \
  --cpu 0.5 \
  --memory 1.0Gi \
  --min-replicas 1 \
  --max-replicas 1
```

### 3. Set environment variables / secrets

The UI needs the API URL and an API key. Since both apps are in the same ACA environment, use the internal URL for API calls:

```bash
az containerapp secret set \
  --name ca-vetcostcheck-ui \
  --resource-group rg-3c-invoice \
  --secrets "api-key=<THE_API_KEY>"

az containerapp update \
  --name ca-vetcostcheck-ui \
  --resource-group rg-3c-invoice \
  --set-env-vars \
    API_BASE_URL=http://ca-invoice-api:8000 \
    API_KEY=secretref:api-key
```

**Important:** Use `http://ca-invoice-api:8000` (internal URL, no TLS) — this bypasses IP restrictions and is faster. Do NOT use the public `https://3cvetcostcheck.flex-capital-scale.com` URL from within the same environment.

### 4. Redeployment script

For subsequent deploys, add to the existing `deploy.sh` or create a separate one:

```bash
az acr build --registry cr3cinvoice --image vetcostcheck-ui:v1 .
az containerapp update \
  --name ca-vetcostcheck-ui \
  --resource-group rg-3c-invoice \
  --image cr3cinvoice.azurecr.io/vetcostcheck-ui:v1
```

---

## Embedding API Docs (Swagger UI)

The PM wants to browse the API's interactive docs from the Streamlit UI. Since the browser can't reach `http://ca-invoice-api` (internal only), the approach is:

1. The Streamlit app fetches `/openapi.json` from the API internally
2. It serves a Swagger UI page using `st.components.v1.html()` that renders the spec client-side

### Code to add in the Streamlit app

```python
import streamlit as st
import requests
import os

API_BASE_URL = os.getenv("API_BASE_URL", "http://ca-invoice-api:8000")

def docs_page():
    st.title("API Documentation")

    # Fetch the OpenAPI spec from the internal API
    try:
        resp = requests.get(f"{API_BASE_URL}/openapi.json", timeout=5)
        spec_json = resp.text
    except Exception as e:
        st.error(f"Could not load API docs: {e}")
        return

    # Render Swagger UI in an iframe-like component
    swagger_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
        <script>
            SwaggerUIBundle({{
                spec: {spec_json},
                dom_id: '#swagger-ui',
                presets: [SwaggerUIBundle.presets.apis],
                layout: "BaseLayout"
            }})
        </script>
    </body>
    </html>
    """
    st.components.v1.html(swagger_html, height=800, scrolling=True)
```

Add this as a page in the Streamlit app's navigation (e.g., a sidebar option called "API Docs").

---

## Authentication

The API uses `X-Api-Key` header authentication. Two keys are configured:

- **Testing key:** For development and the Streamlit UI
- **Production key:** For the production consumer system

The key is set as the `API_KEY` env var on the Streamlit container app. All API calls from the UI should include it as:

```python
headers = {"X-Api-Key": os.getenv("API_KEY")}
requests.post(f"{API_BASE_URL}/process", json={"file_id": fid}, headers=headers)
```

---

## Network Summary

```
PM's browser (any IP)
    │
    ▼
ca-vetcostcheck-ui (Streamlit, public ingress, port 8501)
    │
    │  internal network (cae-3c-invoice)
    │  http://ca-invoice-api:8000
    │  no IP restrictions, no TLS
    ▼
ca-invoice-api (FastAPI, port 8000)
    │
    ▼
Redis, Azure Blob Storage, Azure OpenAI, LandingAI OCR
```

No IP whitelisting is needed for the Streamlit→API connection because they share the same ACA environment.
