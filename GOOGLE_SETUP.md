# Google Maps Setup

The Map page and the location maps in the file detail panel are rendered with the
**Google Maps JavaScript API**, and the "Locate" button in the *New location* dialog
uses the **Geocoding API**. Both are read from two environment variables:

| Variable | What it is |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Browser API key for the Maps JS + Geocoding APIs |
| `GOOGLE_MAPS_MAP_ID` | A Cloud "Map ID" required for Advanced Markers (the pins/badges) |

The backend serves both to the frontend through `GET /config`, so they live only in your
`.env` (never in git). If they are blank, the app still runs — the Map page shows a
"Map unavailable" placeholder and the detail-panel maps are hidden.

This guide produces those two values. It takes ~10 minutes and, for a single-user
archive, stays comfortably inside the free tier (see [Cost & free quota](#cost--free-quota)).

---

## Prerequisites

- A Google account.
- A credit/debit card. Google **requires a billing account** on Maps Platform even though
  your usage will be free. You will not be charged within the free limits below, and you
  can add a budget alert (step 8) as a safety net.

---

## Step 1 — Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. In the top bar, click the **project dropdown → New Project**.
3. Name it e.g. `footage-archive` and click **Create**.
4. Make sure the new project is selected in the project dropdown before continuing.

## Step 2 — Enable billing

1. Navigation menu (☰) → **Billing**.
2. **Link a billing account** (create one if you don't have it — this is where the card
   goes). The project must show "Billing is enabled".

> Without billing, the Maps JS API returns errors and maps render as a grey,
> watermarked "for development purposes only" image.

## Step 3 — Enable the two APIs

1. Navigation menu (☰) → **APIs & Services → Library**.
2. Search **"Maps JavaScript API"** → open it → **Enable**.
3. Go back to the Library, search **"Geocoding API"** → open it → **Enable**.

(That's all the APIs this app uses. You do not need Places, Directions, etc.)

## Step 4 — Create an API key

1. **APIs & Services → Credentials**.
2. **+ Create credentials → API key**.
3. A key like `AIzaSy...` is shown. Copy it — this is your `GOOGLE_MAPS_API_KEY`.
4. Click **Edit API key** (pencil) to restrict it in the next step. *Leaving a Maps key
   unrestricted is the main thing to avoid* — restriction, not secrecy, is what protects a
   browser key (it is always visible in page source).

## Step 5 — Restrict the API key

In the key's edit page:

**Application restrictions → Websites (HTTP referrers).** Add an entry for every URL you
open the app from. Examples:

| Where you use it | Referrer to add |
|---|---|
| Local dev (`ng serve`) | `http://localhost:4200/*` |
| Docker stack on the NAS by IP | `http://192.168.2.230:8080/*` |
| NAS by hostname (if you use one) | `http://nas.local:8080/*` |

> Use the real host/port you browse to. If you reach the app by **both** an IP and a
> hostname, add **both**. Wrong/missing referrers → `RefererNotAllowedMapError` and a blank
> map. (Adjust the port if you changed `FRONTEND_PORT`.)

**API restrictions → Restrict key →** select exactly:
- **Maps JavaScript API**
- **Geocoding API**

Click **Save**. (Restriction changes can take a few minutes to propagate.)

## Step 6 — Create a Map ID (for Advanced Markers)

The custom pins/badges use **Advanced Markers**, which require a Map ID.

1. Navigation menu (☰) → **Google Maps Platform → Map management**
   (direct link: <https://console.cloud.google.com/google/maps-apis/studio/maps>).
2. **Create Map ID**.
3. Name it e.g. `footage-archive-web`, **Map type: JavaScript**, and choose **Vector**
   (recommended — vector maps support Advanced Markers fully).
4. **Save**, then copy the generated **Map ID** — this is your `GOOGLE_MAPS_MAP_ID`.

## Step 7 — Create the map style and attach it to the Map ID

The app wants a calm, POI-free basemap in its own colours (dark/light). Google only applies
a custom style when the map has a Map ID — the `styles` option in the Maps JS API is ignored
once a Map ID is set — so the style has to be created and attached by hand in the Console;
there is no API for this.

1. Navigation menu (☰) → **Google Maps Platform → Map Styles**
   (direct link: <https://console.cloud.google.com/google/maps-apis/studio/styles>).
2. **Create style → JavaScript**. Name it `footage-archive-quiet`.
3. The editor has a **Light mode** and a **Dark mode** variant — edit both (use the mode
   switch at the top of the editor). Apply the settings below to each, substituting that
   mode's colour column.
4. **Save**, then **Publish**.
5. **Google Maps Platform → Map management** → open the Map ID created in Step 6 →
   **Associate a style** → pick `footage-archive-quiet` → **Save**.

Feature settings (same for both modes — only the colour differs):

| Feature | Visibility | Light colour | Dark colour |
|---|---|---|---|
| Points of interest — all categories (attractions, business, government, medical, parks, places of worship, schools, sports): icons & labels | Off | — | — |
| Points of interest — park *areas* (fill only, no labels) | On | `#e4eae4` | `#1a1f1c` |
| Transit — lines and all stations (rail, bus, airport, ferry) | Off | — | — |
| Road — highways & arterials (geometry) | On | `#ffffff` | `#313740` |
| Road — local roads (geometry) | On | `#ffffff` | `#252a31` |
| Road — shields / route numbers | Off | — | — |
| Road — local road labels | Off | — | — |
| Political — country, admin area, locality labels | On | `#59616b` | `#8d949e` |
| Political — neighbourhood labels | On | `#8a919a` | `#6b727c` |
| Political — land parcel | Off | — | — |
| Landscape — land | On | `#eef0f2` | `#181b20` |
| Water | On | `#d5dce2` | `#0b0e12` |
| Labels — text (non-prominent, e.g. local roads/neighbourhoods) | — | `#8a919a` | `#6b727c` |
| Labels — text stroke/halo | — | `#eef0f2` (= land) | `#181b20` (= land) |

Notes on the table:
- "Prominent" labels (countries, cities) get the slightly lighter/brighter shade
  (`#59616b` light / `#8d949e` dark); everything else (local roads, neighbourhoods) uses the
  muted shade (`#8a919a` light / `#6b727c` dark).
- The label stroke (halo) is always set to the same colour as the land fill, so text sits on
  top of the map without a hard outline.
- These colours mirror the app's own design tokens in `frontend/src/styles.css` — if that
  palette ever changes, update the style to match.

## Step 8 — (Recommended) Guard against surprise charges

1. **Billing → Budgets & alerts → Create budget**, set a small amount (e.g. €1) so you get
   an email if anything ever bills.
2. Optional hard cap: **APIs & Services → (each API) → Quotas** lets you cap requests per
   day so you can never exceed the free tier.

## Step 9 — Put the values in your `.env`

Add the two values you copied to the project's `.env` (same file as the DB credentials —
see `.env.example`):

```dotenv
GOOGLE_MAPS_API_KEY=AIzaSy...your key...
GOOGLE_MAPS_MAP_ID=...your map id...
```

- **Local dev:** restart the backend (`uv run python app.py`) so it re-reads `.env`, then
  reload the frontend.
- **Docker:** `docker compose up -d --build` (Compose reads `.env`).

Verify the backend is serving them: open <http://localhost:8051/config> (or
`<host>/api/config` for the Docker stack) and confirm `google_maps_api_key` and
`google_maps_map_id` are populated. Then open the **Map** page — you should see a Google
basemap with your markers.

---

## Cost & free quota

Google replaced the old flat **$200/month credit** (on **1 March 2025**) with **per-SKU
monthly free caps**. The two APIs this app uses are both in the **Essentials** tier:

| API (SKU) | Free per month | What counts as one unit |
|---|---|---|
| Maps JavaScript API — *Dynamic Maps* | **10,000 map loads** | one *map load* = the map being instantiated (opening the Map page, or a detail/new-location map) |
| Geocoding API | **10,000 requests** | one "Locate" geocode |

- **Panning and zooming cost nothing** — only the initial map load is billed. The marker
  reloads while you pan call *this app's own backend* (`/locations/map-points`), not Google,
  so they are always free.
- For a single-user archive you'll do, at most, tens-to-hundreds of map loads and a handful
  of geocodes per month — effectively **$0**, far below both 10,000 caps.
- Authoritative, current pricing:
  <https://developers.google.com/maps/billing-and-pricing/overview>.

---

## Troubleshooting

Open the browser devtools **Console** — Google prints a specific error name:

| Symptom / console error | Cause & fix |
|---|---|
| Grey map, *"for development purposes only"* watermark | Billing not enabled (step 2). |
| `RefererNotAllowedMapError` | The URL you're on isn't in the key's HTTP-referrer list (step 5). Add the exact `scheme://host:port/*`. |
| `ApiNotActivatedMapError` | Maps JavaScript API not enabled, or the key's API restriction doesn't include it (steps 3 & 5). |
| `InvalidKeyMapError` / `ApiTargetBlockedMapError` | Wrong key, or API restrictions exclude this API. |
| Map shows but pins/badges are missing; console warns about Advanced Markers / Map ID | `GOOGLE_MAPS_MAP_ID` is missing or not a *Vector* JS Map ID (step 6). |
| "Locate" button never finds anything | Geocoding API not enabled or not in the key's API restrictions (steps 3 & 5). |
| "Map unavailable" placeholder on the Map page | `GOOGLE_MAPS_API_KEY` is blank in `.env`, or `/config` isn't returning it — confirm the backend restarted. |
| POIs/transit still show, or colours look unchanged, right after editing/publishing the style | Normal — a published style change can take a few minutes to roll out, and the browser also caches map tiles. Wait a bit, then hard-reload (or reload in a private window) before assuming the edit didn't take (step 7). |

---

## Disabling maps

Leave `GOOGLE_MAPS_API_KEY` (and `GOOGLE_MAPS_MAP_ID`) blank. The app runs normally; the
Map page shows the placeholder and the detail-panel maps are simply not rendered.
