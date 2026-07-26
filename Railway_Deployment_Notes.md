# Steno Desk Backend — Railway Deployment Notes

**Repository:** https://github.com/sheffk78/steno-desk-app
**Local Path:** `/tmp/steno-desk-app/backend`
**Status:** ✅ Code committed and pushed to GitHub

---

## Files Created/Modified

### 1. Dockerfile (Python pattern per railway-deploy skill)
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

**Key points:**
- Uses `python:3.11-slim` base image
- Installs dependencies from `requirements.txt` with `--no-cache-dir`
- Uses `sh -c` for `$PORT` expansion (Railway requirement)
- No `railway.json` startCommand (Dockerfile CMD handles startup)
- Builder: DOCKERFILE (via railway.json)

### 2. railway.json
```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE"
  }
}
```

### 3. .env.example
Contains all required environment variables:
- `MONGO_URL` (or `DATABASE_URL`)
- `DB_NAME`
- `POSTMARK_SERVER_TOKEN`
- `POSTMARK_FROM_EMAIL`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PUBLIC_KEY` (optional but recommended)
- `JWT_SECRET`
- `SENDER_EMAIL`
- `CORS_ORIGINS` (optional)
- `DISABLE_SCHEDULER` (optional)

### 4. server.py
Added `/api/health` health check endpoint for Railway auto-deploy:
```python
@api.get("/health")
async def health():
    """Health check endpoint for Railway auto-deploy."""
    return {"status": "healthy", "app": "Steno Desk"}
```

---

## Deployment Status

### ✅ Completed (Local)
- [x] Dockerfile created with Python pattern
- [x] railway.json configured for DOCKERFILE builder
- [x] .env.example with all required env vars
- [x] Health check endpoint added
- [x] Code committed to GitHub

### ⏸️ Blocked by Model Gate
The following Railway GraphQL operations require a model that meets the railway-deploy skill's **HARD MODEL GATE** (DeepSeek V4 Pro or GPT-5.5 ONLY):

- **Service creation** (`serviceCreate` mutation)
- **Environment variable setup** (`variableUpsert` or `variableCollectionUpsert`)
- **Healthcheck path configuration** (`serviceInstanceUpdate` mutation)
- **Auto-deploy enablement** (`serviceInstanceAutoDeployUpdate` mutation)

Current model: `z-ai/glm-4.7-flash` (GLM) — ❌ Non-reasoning model, cannot execute Railway GraphQL

---

## Next Steps (Requires GPT-5.5 or DeepSeek V4 Pro)

Once a gated model is available:

### 1. Create Railway Service
Query existing project/service/environment IDs (provided by database agent), then:
```graphql
mutation {
  serviceCreate(
    input: {
      name: "stenodesk-backend"
      projectId: "PID"
      region: "aws"
      plan: "scalable"
    }
  ) {
    id name
  }
}
```

### 2. Set Environment Variables
```graphql
mutation {
  variableCollectionUpsert(input: {
    projectId: "PID"
    serviceId: "SID"
    environmentId: "EID"
    variables: {
      "MONGO_URL": "${{Postgres.DATABASE_URL}}"
      "DB_NAME": "stenodesk"
      "STRIPE_SECRET_KEY": "sk_..."
      "STRIPE_WEBHOOK_SECRET": "whsec_..."
      "POSTMARK_SERVER_TOKEN": "..."
      "JWT_SECRET": "..."
      "SENDER_EMAIL": "steno@stenodesk.com"
    }
    skipDeploys: false
  }) {
    id
  }
}
```

### 3. Configure Healthcheck Path
```graphql
mutation {
  serviceInstanceUpdate(
    serviceId: "SID"
    environmentId: "EID"
    input: {
      healthcheckPath: "/api/health"
    }
  ) {
    id healthcheckPath
  }
}
```

### 4. Enable Auto-Deploy
```graphql
mutation {
  serviceInstanceAutoDeployUpdate(input: {
    projectId: "PID"
    environmentId: "EID"
    serviceId: "SID"
    enabled: true
  }) {
    enabled
  }
}
```

### 5. Verify Deployment
- Push to main branch will trigger auto-deploy
- Monitor deployments via GraphQL: `deployments(last: 1)`
- Check logs: `deploymentLogs(deploymentId: "DEP_ID")`

---

## Connection Links (Once Service ID Known)
- Database Service ID: **[TO BE PROVIDED BY DATABASE AGENT]**
- Environment ID: **[TO BE PROVIDED BY DATABASE AGENT]**
- Project ID: **[INHERITED FROM DATABASE PROJECT]**

---

## Quick Local Verification
To verify the health endpoint locally before deploy:
```bash
cd /tmp/steno-desk-app/backend
uvicorn server:app --reload

# Test health endpoint
curl http://localhost:8000/api/health
# Expected: {"status": "healthy", "app": "Steno Desk"}
```