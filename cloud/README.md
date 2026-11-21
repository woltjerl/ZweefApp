# Hosted thermal briefing (Google Cloud)

A static website with today's briefing for EHHV, rebuilt every night at 00:05 Dutch time.

```
Cloud Scheduler 00:05 ──► Cloud Run job (cloud/job.py, 1 vCPU / 1 GiB, ~15 s)
                            │ reads   gs://<project>-thermal-data/pack     history pack, private (~13 MB)
                            │ fetches Open-Meteo forecast for today
                            ▼ writes  gs://<project>-thermal-briefing/     public website
                                      index.html, latest.json, <date>/…
Website: https://storage.googleapis.com/<project>-thermal-briefing/index.html
```

What the site shows:

- **Day forecast** — rating, thermal window, typical climb, how high climbs go, cumulus base,
  wind, chance to find the first thermal after a winch launch, hour by hour (08–19 h).
- **Briefing map** per time window (9–12, 12–15, 15–18 h) and runway: reach, where first
  thermals were found in similar wind (150 m cells), spots A/B/C, best pilots' first climbs,
  airspace. Runway buttons are ordered by likelihood in the forecast wind; the most likely
  opens first, the others are one tap away. Works on a phone (the briefing is a bottom sheet).
- Links keep the view: `index.html#w=midday&r=25`, `index.html#v=day`. Each day also stays at
  `<date>/index.html` for 60 days.

## Cost

Everything runs inside Google Cloud's free tiers (check the current limits on the pricing pages):

| Part | Use per month | Free tier |
|---|---|---|
| Cloud Run job | ~30 runs × ~15 s × 1 vCPU, 1 GiB | 180,000+ vCPU-seconds |
| Cloud Scheduler | 1 job | 3 jobs per billing account |
| Cloud Storage (us-central1) | ~15 MB pack + ~2 MB per day (deleted after 60 days) | 5 GB in US regions |
| Download traffic | ~1.5 MB per page view | up to 100 GB from US regions |
| Cloud Build | one build per deploy (~3 min) | 2,500 build-minutes |
| Artifact Registry | one image (~400 MB; cleanup policy keeps 2) | 0.5 GB |

Expect €0–0.10 a month. Billing must be enabled on the project anyway (Cloud Run requires it):
set a budget alert of €1 under *Billing → Budgets & alerts*.

The bucket is in `us-central1` because the Cloud Storage free tier only covers US regions;
for a static page the extra distance (~100 ms) does not matter.

## Set up (once)

Needs: a Google Cloud project with billing, and `gcloud` logged in with an account that owns it.
If your active gcloud account is a work account, pass your own with `ACCOUNT=`; the script names
the project and account on every command and does not change your gcloud defaults.

```bash
gcloud auth login you@gmail.com            # once, if needed
PROJECT=my-gliding-project ACCOUNT=you@gmail.com ME="Woltjer" ./cloud/deploy.sh
```

`deploy.sh` (safe to re-run) enables the APIs, creates the two buckets (site public, pack
private), the service accounts, uploads the history pack, builds the image with Cloud Build,
creates the Cloud Run job and the nightly schedule, runs the job once and prints the website URL.

Settings (environment variables): `REGION` (us-central1), `SITE_BUCKET`, `DATA_BUCKET`, `JOB`,
`SCHEDULE` (`5 0 * * *`, Europe/Amsterdam), `ME` (your logbook name, see Privacy).

## Keep it up to date

The nightly job only needs a fresh forecast. The history (flights, models) changes when you run
`update_flights.py`; publish it afterwards:

```bash
.venv/bin/python scripts/update_flights.py --publish-pack gs://my-gliding-project-thermal-data/pack \
    --me "Woltjer" --gcloud-account you@gmail.com
# or set ZWEEF_PACK_URI, ZWEEF_ME, ZWEEF_GCLOUD_ACCOUNT once in your shell profile
```

After code changes, re-run `./cloud/deploy.sh` (rebuilds the image). Run the job by hand:

```bash
gcloud run jobs execute thermal-briefing --region us-central1 --project my-gliding-project --wait
```

When you retrain with newer scikit-learn/numpy versions, update `cloud/requirements.txt` to the
same versions (the models are pickled) and redeploy.

## Privacy

- The pack has no names: only the logbook's numeric pilot ids, launches, first climbs and models.
- The image contains only the briefing code (see `Dockerfile` and `.gcloudignore`): no database,
  no logbook credentials (`src/data_collection` is not uploaded).
- The website is public to anyone with the link. It shows the best pilots' first climbs without
  names. With `ME=` it also shows **your** first thermals and your connect rate in these
  conditions; leave `ME` empty if you do not want that on a public page.

## Test locally (no Google Cloud)

```bash
.venv/bin/python scripts/publish_briefing_pack.py --out /tmp/pack --me "Woltjer"
.venv/bin/python cloud/job.py --pack /tmp/pack --site /tmp/briefing_site
open /tmp/briefing_site/index.html
```

## Troubleshooting

- *Build fails with a permission error*: new projects run Cloud Build as the default compute
  service account; `deploy.sh` grants it `roles/cloudbuild.builds.builder`. Wait a minute and re-run.
- *Scheduler gets 403*: the scheduler account needs `roles/run.invoker` on the job (deploy.sh sets it).
- *Organisation policy blocks public buckets* (`allUsers`): use a personal project, not one inside
  a company organisation.
- *Logs*: Cloud Console → Cloud Run → Jobs → thermal-briefing → Logs.
