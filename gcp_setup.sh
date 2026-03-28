#!/bin/bash
# One-time GCP setup for the email agent.
# Usage: PROJECT_ID=your-project-id REGION=us-central1 bash gcp_setup.sh

set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID before running this script}"
: "${REGION:=us-central1}"

SA_NAME="inbox-assassin-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="gcr.io/${PROJECT_ID}/inbox-assassin"
BUCKET="${PROJECT_ID}-inbox-assassin-logs"
JOB_NAME="inbox-assassin"

echo "==> Project: $PROJECT_ID  Region: $REGION"
gcloud config set project "$PROJECT_ID"

# ── Enable required APIs ──────────────────────────────────────────────────────
echo "==> Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com

# ── Service account ───────────────────────────────────────────────────────────
echo "==> Creating service account..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="InboxAssassin" || true

for role in \
  roles/secretmanager.secretAccessor \
  roles/storage.objectAdmin \
  roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role"
done

# ── Secrets ───────────────────────────────────────────────────────────────────
echo "==> Uploading secrets to Secret Manager..."

[ -f token.json ]       && gcloud secrets create gmail-token       --data-file=token.json       || gcloud secrets versions add gmail-token       --data-file=token.json
[ -f credentials.json ] && gcloud secrets create gmail-credentials --data-file=credentials.json || gcloud secrets versions add gmail-credentials --data-file=credentials.json

# Gemini API key — reads from .env or prompts
GEMINI_KEY="${GEMINI_API_KEY:-}"
if [ -z "$GEMINI_KEY" ] && [ -f .env ]; then
  GEMINI_KEY=$(grep GEMINI_API_KEY .env | cut -d= -f2)
fi
if [ -z "$GEMINI_KEY" ]; then
  read -rsp "Enter your Gemini API key: " GEMINI_KEY; echo
fi
echo -n "$GEMINI_KEY" | gcloud secrets create gemini-api-key --data-file=- || \
echo -n "$GEMINI_KEY" | gcloud secrets versions add gemini-api-key --data-file=-

# ── Cloud Storage bucket for logs ─────────────────────────────────────────────
echo "==> Creating GCS bucket..."
gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://${BUCKET}" || true

# ── Build and push container image ───────────────────────────────────────────
echo "==> Building container image..."
gcloud builds submit --tag "$IMAGE"

# ── Create Cloud Run Job ──────────────────────────────────────────────────────
echo "==> Creating Cloud Run Job..."
gcloud run jobs create "$JOB_NAME" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --set-env-vars="MODEL_BACKEND=gemini,GCS_BUCKET=${BUCKET},GCP_PROJECT=${PROJECT_ID}" \
  --task-timeout=3600 \
  || gcloud run jobs update "$JOB_NAME" \
    --image="$IMAGE" \
    --region="$REGION" \
    --set-env-vars="MODEL_BACKEND=gemini,GCS_BUCKET=${BUCKET},GCP_PROJECT=${PROJECT_ID}"

# ── Cloud Scheduler — every 4 hours ──────────────────────────────────────────
echo "==> Creating Cloud Scheduler job (every 4 hours)..."
gcloud scheduler jobs create http "${JOB_NAME}-schedule" \
  --location="$REGION" \
  --schedule="0 */4 * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA_EMAIL" \
  || gcloud scheduler jobs update http "${JOB_NAME}-schedule" \
    --location="$REGION" \
    --schedule="0 */4 * * *"

echo ""
echo "✓ Setup complete!"
echo ""
echo "  To trigger the job manually:"
echo "    gcloud run jobs execute $JOB_NAME --region=$REGION"
echo ""
echo "  To view logs:"
echo "    gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=$JOB_NAME' --limit=50"
