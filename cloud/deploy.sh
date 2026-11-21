#!/usr/bin/env bash
# One-time setup (safe to re-run) of the hosted thermal briefing on Google Cloud.
# See cloud/README.md. Every command names the project explicitly; your gcloud defaults are not changed.
#
#   PROJECT=my-gliding-project ./cloud/deploy.sh
#   PROJECT=my-gliding-project ACCOUNT=me@gmail.com ME="Woltjer" ./cloud/deploy.sh
set -euo pipefail

: "${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-us-central1}"                      # Cloud Storage free tier is US-only
SITE_BUCKET="${SITE_BUCKET:-${PROJECT}-thermal-briefing}"
DATA_BUCKET="${DATA_BUCKET:-${PROJECT}-thermal-data}"
JOB="${JOB:-thermal-briefing}"
SCHEDULE="${SCHEDULE:-5 0 * * *}"                    # 00:05 Dutch time
ME="${ME:-}"
REPO=briefing
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/job:latest"
JOB_SA="thermal-briefing-job@$PROJECT.iam.gserviceaccount.com"
SCHED_SA="thermal-briefing-sched@$PROJECT.iam.gserviceaccount.com"

cd "$(dirname "$0")/.."
gc() { gcloud --project="$PROJECT" ${ACCOUNT:+--account="$ACCOUNT"} --quiet "$@"; }

echo "== Enable APIs"
gc services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com

echo "== Buckets: $SITE_BUCKET (public website), $DATA_BUCKET (private history pack)"
gc storage buckets describe "gs://$SITE_BUCKET" >/dev/null 2>&1 || \
  gc storage buckets create "gs://$SITE_BUCKET" --location="$REGION" --uniform-bucket-level-access
gc storage buckets add-iam-policy-binding "gs://$SITE_BUCKET" --member=allUsers --role=roles/storage.objectViewer >/dev/null
gc storage buckets update "gs://$SITE_BUCKET" --lifecycle-file=cloud/lifecycle.json   # old days deleted after 60 days
gc storage buckets describe "gs://$DATA_BUCKET" >/dev/null 2>&1 || \
  gc storage buckets create "gs://$DATA_BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention

echo "== Service accounts"
gc iam service-accounts describe "$JOB_SA" >/dev/null 2>&1 || \
  gc iam service-accounts create thermal-briefing-job --display-name="Thermal briefing job"
gc iam service-accounts describe "$SCHED_SA" >/dev/null 2>&1 || \
  gc iam service-accounts create thermal-briefing-sched --display-name="Thermal briefing scheduler"
gc storage buckets add-iam-policy-binding "gs://$SITE_BUCKET" --member="serviceAccount:$JOB_SA" --role=roles/storage.objectAdmin >/dev/null
gc storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" --member="serviceAccount:$JOB_SA" --role=roles/storage.objectViewer >/dev/null

echo "== History pack"
.venv/bin/python scripts/publish_briefing_pack.py --uri "gs://$DATA_BUCKET/pack" --project "$PROJECT" \
  ${ACCOUNT:+--account "$ACCOUNT"} ${ME:+--me "$ME"}

echo "== Image (built by Cloud Build from the files listed in .gcloudignore)"
gc artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
  gc artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"
gc artifacts repositories set-cleanup-policies "$REPO" --location="$REGION" --policy=cloud/ar-cleanup.json --no-dry-run \
  || echo "   (cleanup policy not set; old images then cost a few cents a month)"
PROJECT_NUMBER=$(gc projects describe "$PROJECT" --format='value(projectNumber)')
gc projects add-iam-policy-binding "$PROJECT" --condition=None \
  --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role=roles/cloudbuild.builds.builder >/dev/null                # builds run as this account in new projects
gc builds submit --tag "$IMAGE" .

echo "== Cloud Run job"
gc run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" --service-account "$JOB_SA" \
  --memory 1Gi --cpu 1 --task-timeout 20m --max-retries 2 \
  --set-env-vars "PACK_URI=gs://$DATA_BUCKET/pack,SITE_URI=gs://$SITE_BUCKET"

echo "== Nightly schedule ($SCHEDULE Europe/Amsterdam)"
gc run jobs add-iam-policy-binding "$JOB" --region "$REGION" --member="serviceAccount:$SCHED_SA" --role=roles/run.invoker >/dev/null
URI="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run"
if gc scheduler jobs describe "$JOB-nightly" --location "$REGION" >/dev/null 2>&1; then verb=update; else verb=create; fi
gc scheduler jobs "$verb" http "$JOB-nightly" --location "$REGION" --schedule "$SCHEDULE" \
  --time-zone "Europe/Amsterdam" --uri "$URI" --http-method POST --oauth-service-account-email "$SCHED_SA"

echo "== First run"
gc run jobs execute "$JOB" --region "$REGION" --wait
echo
echo "Website: https://storage.googleapis.com/$SITE_BUCKET/index.html"
