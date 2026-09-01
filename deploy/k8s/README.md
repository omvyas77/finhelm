# Kubernetes manifests

**These are validated against a local `kind` cluster. The live demo does not run on
Kubernetes.** For single-user demo traffic, a cluster is unjustified cost and operational
overhead. They exist so that the scaling path is concrete rather than hypothetical, and
claiming a k8s deployment that isn't running is trivially caught in the first follow-up
question.

| file | what it is |
|---|---|
| `00-namespace.yaml` | |
| `01-configmap.yaml` | non-secret wiring: OTLP endpoint, data dir, thread pinning |
| `02-secret.yaml` | template with **no values** — see below |
| `03-pvc.yaml` | the 961 MB FAISS index, mounted rather than baked or rebuilt |
| `04-deployment.yaml` | 2 replicas, non-root, three distinct probes |
| `05-service.yaml` | ClusterIP |
| `06-hpa.yaml` | CPU-based, 2–6 replicas |
| `07-pdb.yaml` | keeps one replica through a node drain |

## What was actually verified

On `kind` v0.33 / Kubernetes v1.34, then torn down:

- all seven manifests accepted by a real API server (`kubectl apply --dry-run=server`)
- Deployment reached `Available 1/1` and the pod `Running`
- `PersistentVolumeClaim` **Bound**, and `/data` genuinely read-only inside the container
- `securityContext` took effect — `uid=10001 gid=10001`, non-root
- `envFrom` wired the ConfigMap through: `OMP_NUM_THREADS=1`, `FINHELM_DATA_DIR=/data`,
  the OTLP endpoint
- the Service's EndpointSlice selected the pod with `ready=true`

Two changes were needed to run it on kind, and both are properties of the validation
cluster rather than of the manifests:

1. **`ReadOnlyMany` → `ReadWriteOnce`.** See below; this one is worth reading.
2. **Resource requests lowered, and the image swapped for `busybox`.** The Deployment
   requests a measured 3 GiB — a single `/ask` peaks at 2.694 GiB with both models
   resident plus the index — and the kind node had 2.83 GiB allocatable, so the scheduler
   correctly refused: `0/1 nodes are available: 1 Insufficient memory`. The real image is
   5.76 GB and was never pushed to a registry, so the structural check ran on `busybox`
   with the probes removed. **The container itself was therefore not exercised here** —
   that is what the compose stack in the repo root is for, and it is verified end to end.

## The access-mode deadlock, because nothing tells you

`ReadOnlyMany` is correct for production: every replica reads the same immutable index and
none writes it, which is what lets the HPA scale past one pod. On GKE that wants Filestore
and on EKS an EFS volume.

kind's default StorageClass is `rancher.io/local-path`, which cannot serve it. The failure
is silent in a specific and unhelpful way:

- the PVC stays `Pending` forever, reporting only `waiting for first consumer to be created`
- every pod stays `Pending`
- **no event on either object ever mentions the access mode**

It reads like a scheduling problem and isn't. Confirmed by changing exactly one field: an
otherwise identical PVC with `ReadWriteOnce` bound in six seconds and its pod went Ready.

## The Secret

`02-secret.yaml` is committed with `stringData: {}` on purpose, so applying it can never
overwrite real keys with blanks. Create the real one out of band:

```bash
kubectl -n finhelm create secret generic finhelm-secrets \
  --from-literal=ANTHROPIC_API_KEY=... \
  --from-literal=GOOGLE_API_KEY=...
```

A Secret is base64, not encryption. Beyond a demo this should be an External Secrets
Operator reference or a KMS-backed CSI driver, so the value never sits in etcd in a form
an etcd read can recover.

## Known gaps

- **The HPA scales on CPU, which is the wrong signal.** What saturates this service is
  concurrent in-flight questions; the cross-encoder is CPU-bound and pinned to one thread,
  so a pod is saturated while its CPU request still reads as half idle. The 60% target is
  a deliberate under-shoot to compensate. A request-rate or queue-depth metric through the
  custom metrics API is the correct answer and needs an adapter this demo does not run.
- **No Ingress or TLS.** ClusterIP only; exposure is left to the cluster's own ingress.
- **No NetworkPolicy.** Everything in the namespace can reach everything else.
- **The index PVC has no population strategy.** Something has to put 961 MB on the volume
  before the first pod is Ready; a Job that runs `scripts/build_index.py`, or a
  pre-populated snapshot, is the missing piece.
