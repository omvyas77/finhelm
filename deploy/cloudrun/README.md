# Cloud Run configs (not deployed)

Configs for running the API on Cloud Run. **None of these commands has been run**, and no
Cloud Run service exists; see the status section at the end. The live demo is the Hugging
Face Space in `../hf-space/`, which runs the whole pipeline in-process and does not use
this service.

## Why Cloud Run

Scale-to-zero on a generous free tier, and no cluster to operate. The k8s manifests in
`../k8s/` exist to make the scaling path concrete, but running a cluster for demo traffic
would be cost and operational overhead with nothing to show for it.

## The two commands

```bash
# 1. Build in Google's infrastructure. The image is 5.76 GB; pushing that from a home
#    connection is 20-40 minutes, and Cloud Build keeps it inside Google's network.
gcloud builds submit --config deploy/cloudrun/cloudbuild.yaml

# 2. Deploy. Substitute PROJECT_ID and BUCKET in service.yaml first.
gcloud run services replace deploy/cloudrun/service.yaml --region us-central1
```

Prerequisites, in the order they bite:

```bash
gcloud artifacts repositories create finhelm --repository-format=docker --location=us-central1
gsutil mb -l us-central1 gs://YOUR_BUCKET                       # the index lives here
gsutil -m cp -r data/index/filings_semantic_ctx_bge-base-en-v15 gs://YOUR_BUCKET/index/
printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key --data-file=-
printf '%s' "$GOOGLE_API_KEY"    | gcloud secrets create google-api-key    --data-file=-
```

## Decisions worth explaining

**The index is a GCS FUSE mount, not baked into the image.** 961 MB that changes on a
different cadence than the code, and baking it means every code change re-pushes it. The
mount is read-only because nothing writes it and a read-only mount makes that structural.

**`containerConcurrency: 1`.** The default is 80. This service holds an embedding model
and a cross-encoder in memory and peaks at 2.694 GiB serving a single question — two
concurrent requests on one instance is an OOM, not throughput. Cloud Run's own scaling
handles concurrency by adding instances.

**`minScale: 0`.** Scale-to-zero is why this fits a free tier, and the cost is a cold
start of image pull plus ~20 s of model load. That would be a deliberate trade for a
low-traffic demo, and would need stating wherever the URL is shared so a cold service is
not read as a broken one.

## Status: written, not deployed

No GCP project or billing account exists for this yet, so **none of these commands have
been run** and no live URL exists. What is verified is the thing underneath: the same
image runs the five-service compose stack locally and answers real questions end to end
with citations. What is unverified is everything specific to Cloud Run — GCS FUSE mount
behaviour under `gen2`, actual cold-start time, and whether the 5.76 GB image pull sits
inside the request timeout on a cold instance.

That last one is the risk worth naming: a 5.76 GB image is large for scale-to-zero, and if
cold pulls prove too slow the honest fixes are a serving-only image without the eval
harness, or `minScale: 1` and paying for it.
