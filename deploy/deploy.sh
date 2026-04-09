#!/bin/bash
set -e

# =============================================================================
# Deploy Script - notorios-odoo-poller-orders-MS
# Uses gcloud compute ssh/scp — no raw SSH key required.
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# -----------------------------------------------------------------------------
# Config (non-secret deploy vars — no encryption needed)
# -----------------------------------------------------------------------------

GCP_PROJECT="notorios"
GCP_REGION="us-central1"
GCP_ZONE="us-central1-c"
VM_NAME="langgraph"
VM_DEPLOY_PATH="/opt/odoo-poller-microservice"
AR_IMAGE="us-central1-docker.pkg.dev/notorios/odoo-poller/notorios-odoo-orders-sync-ms"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE_PROD_FILE="$PROJECT_ROOT/docker-compose.prod.yml"

# -----------------------------------------------------------------------------
# Pre-flight
# -----------------------------------------------------------------------------

log_info "Checking gcloud auth..."
if ! gcloud auth print-access-token &>/dev/null; then
    log_error "Not authenticated. Run: gcloud auth login"
    exit 1
fi

log_info "Checking VM deploy path..."
if ! gcloud compute ssh "$VM_NAME" --zone="$GCP_ZONE" --command="test -d $VM_DEPLOY_PATH" 2>/dev/null; then
    log_error "Deploy path $VM_DEPLOY_PATH does not exist on VM $VM_NAME"
    exit 1
fi

log_success "Pre-flight OK"

# -----------------------------------------------------------------------------
# Build image
# -----------------------------------------------------------------------------

log_info "Building image..."
cd "$PROJECT_ROOT"
docker build --platform linux/amd64 -t "${AR_IMAGE}:latest" .
log_success "Image built: ${AR_IMAGE}:latest"

# -----------------------------------------------------------------------------
# Push to Artifact Registry
# -----------------------------------------------------------------------------

log_info "Configuring Docker for Artifact Registry..."
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

log_info "Pushing image..."
docker push "${AR_IMAGE}:latest"
log_success "Image pushed"

# -----------------------------------------------------------------------------
# Copy docker-compose to VM (requires sudo — upload to /tmp then move)
# -----------------------------------------------------------------------------

log_info "Copying docker-compose.prod.yml to VM..."
gcloud compute scp "$COMPOSE_PROD_FILE" "${VM_NAME}:/tmp/docker-compose-poller.yml" --zone="$GCP_ZONE"
gcloud compute ssh "$VM_NAME" --zone="$GCP_ZONE" --command="sudo mv /tmp/docker-compose-poller.yml $VM_DEPLOY_PATH/docker-compose.yml"
log_success "docker-compose.yml updated on VM"

# -----------------------------------------------------------------------------
# Deploy on VM
# -----------------------------------------------------------------------------

log_info "Deploying on VM..."
gcloud compute ssh "$VM_NAME" --zone="$GCP_ZONE" --command="
  set -e
  cd $VM_DEPLOY_PATH

  gcloud auth configure-docker ${GCP_REGION}-docker.pkg.dev --quiet 2>/dev/null || true

  docker pull ${AR_IMAGE}:latest

  export IMAGE_NAME='${AR_IMAGE}:latest'

  docker compose down --remove-orphans || true
  docker compose up -d

  docker image prune -f

  echo '--- Container status ---'
  docker compose ps
  echo '--- Logs (last 30) ---'
  docker compose logs --tail=30
"
log_success "Deploy complete"
