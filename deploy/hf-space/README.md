---
title: finhelm
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: streamlit
sdk_version: 1.62.0
app_file: app.py
pinned: false
---

# finhelm — Streamlit demo

This directory holds what a Hugging Face Space needs. The YAML block above is the Space
config; HF reads it from the README's front matter, which is why it lives here rather than
in a config file.

## How this is wired

The Space runs **only the UI**. `app.py` calls the API over HTTP when `FINHELM_API_URL`
is set and imports the pipeline in-process otherwise — the Space sets it, and points at
the Cloud Run service.

That split is not incidental. A Space would otherwise need the 961 MB FAISS index and
1.5 GB of model weights on its own disk, and would load a second copy of both models into
memory. Keeping retrieval on Cloud Run means one copy of the index, one copy of the
models, and a UI that is a few hundred MB of Streamlit.

## Setup

1. Create the Space with SDK **streamlit**.
2. Add `FINHELM_API_URL` as a **Space variable** (not a secret — it is a public URL).
3. Add nothing else. The Space holds no API keys; the model keys live on Cloud Run, which
   is the only thing that calls a model. A key in a Space's environment is one
   `os.environ` away from anything running in that Space.
4. Push `app.py` and this README to the Space repo.

## Cold starts, and why the demo may look broken

Two of them stack, and a recruiter clicking a link deserves to know before they conclude
the thing is broken:

- **The Space sleeps** after inactivity on the free tier. First hit is ~30 s of container
  start.
- **Cloud Run scales to zero.** The first question after idle pays an image pull plus
  ~20 s of model load, so the first answer can take **60–90 seconds**. Subsequent ones are
  the measured p50 of ~25 s, most of which is retrieval and generation rather than boot.

Setting Cloud Run's `minScale` to 1 removes the second one and costs money continuously,
which is the wrong trade for a portfolio demo.

## Not yet deployed

**Nothing here has been run.** No GCP project, billing account, or HF Space exists for
this yet — those need an account holder, not a build script. The configs are written to
the point where `gcloud builds submit` and `gcloud run services replace` are the next two
commands, and they have not been executed, so treat the cold-start numbers above as
derived from local measurement rather than observed in production.

The build guide's advice stands and is not yet satisfied: **test the live URL from a phone
on cell data before calling it done.** Something always turns out to depend on a local
file path, and a laptop on the same network as the developer is the one client that will
never reveal it.
