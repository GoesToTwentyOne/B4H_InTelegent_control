# B4H portal: workflow and API flow

Django admin portal that reads everything live from the MegCube B4H box. Arrows show who calls whom; each table lists parameters in and data out.

## 1. Staff user (browser)

Logs in at `/admin/` (Django session cookie + CSRF). Every route below is staff-only and checked against a per-page `view_*` permission.

↓ **HTTP GET / POST** to the portal ↓

## 2a. Portal pages (Django admin changelists)

GET with query-string filters. Time fields are `YYYY-MM-DDTHH:MM:SS` (default last 24 h, logs 7 days). Returns rendered HTML.

| Page URL | Params (in) | Box call | Data out |
| --- | --- | --- | --- |
| `/admin/boxapp/recognitionrecord/` | start, end, minor=`face_comparison_successful`\|`stranger`, size 10/20/30, page, hide_deleted | alarm_history (+ person/query if hide_deleted) | Rows: person, score, time, images |
| `/admin/boxapp/capturerecord/` | start, end, target_type=all\|face\|body, size, page | alarm_history (+ structure type) | Rows: device name, attributes, images |
| `/admin/boxapp/person/` | q (name or id) | person/query | person_id + name list |
| `/admin/boxapp/device/` | none | device_config | device_id + name |
| `/admin/boxapp/liveview/` | cols 1-4 | device_config | Grid of `<img src=live/ID/stream/>` |
| `/admin/boxapp/logview/` | start, end, size, page | device_maintenance/log | Log rows + total |
| `/admin/boxapp/algorithmpackage/` | none | alg_warehouse/packet_list | Package name, version, status |

↓ thumbnails in those pages load through the image proxy ↓

## 2b. Portal helper routes (non-admin views)

| Route | In | Out | Notes |
| --- | --- | --- | --- |
| GET `/admin/box-image/` | uri (no `://` or `..`) | image bytes, cached 1 day | Django adds the box cookie; tries `/web/<uri>` then `get_image`; 404/503 on failure |
| GET `/admin/live/<id>/stream/` | device_id | MJPEG `multipart/x-mixed-replace` | Needs `view_liveview`; works in a plain img tag |
| GET `/admin/live/<id>/snapshot/` | device_id | one JPEG | 504 if camera silent (8 s) |
| GET `/admin/box-log/download/` | none | file attachment (zip/json/txt) | Needs `view_logview` |
| GET `/admin/movement-alarm/` | none | HTML: rules, last 100 events, devices |  |
| POST `/admin/movement-alarm/create/` | JSON: name, device_id, zone_points \[\[x,y\]×3+\], sensitivity 0.015, trigger_frames 3, cooldown_seconds 30, alarm_duration_seconds 10; header X-CSRFToken | `{ok, id}` or 400 `{error}` | Saves rule, starts worker |
| POST `.../<rule_id>/toggle/` and `/delete/` | rule_id | redirect | Start/stop worker |
| POST `.../event/<id>/ack/` | event_id | redirect | Sets acknowledged |

↓ services.py builds the request, `box.call()` sends it ↓

## 3. B4HClient (one shared connection, thread lock)

1. GET `/auth/login/challenge?username=` returns session_id, salt, challenge
2. POST `/auth/login` with `{session_id, username, password = sha256(pwd+salt+challenge)}`
3. Every call sends `Cookie: sessionID=...`

1. Reply envelope: `{code, message, data}`; code 0 returns data
2. Code 512 (session dropped, \~30 s idle): login again and retry once
3. One request at a time (box rejects parallel queries); other codes raise B4HError, shown as a page error

↓ HTTPS JSON (`B4H_BASE_URL`, TLS verify off) ↓

## 4. B4H box WebAPI

Max 30 records per request. Timestamps: alarms in ms, logs in seconds, both as strings.

| Method + path | Request body / params | Response data |
| --- | --- | --- |
| POST `/device_access/device_config` | {offset:0, size:100} | list of {device_id, device_name, rtsp_param.url} |
| POST `/face_manager/person/query` | {offset, size≤30, get_feature:false} | {person_list\[{person_id, person_info.name}\], total_count} |
| POST `/device_alarm/alarm_history` | {offset, size, query_condition:{start_time, end_time, alarm_type:\[{major_type:"face_basic_business", minor_type:\[...\]}\]}}. Captures add {major_type:"structure", minor_type:\[face, pedestrian, vehicle, non_motor, plate\]} | {list or alarm_list, total_count}: additional, global_info.time_ms, faces/pedestrians, full_images, recognition_info |
| GET `/web/<uri>` or `/device_storage/get_image?image_uri=` | image uri from a record | raw JPEG (content-type often unlabeled) |
| POST `/device_maintenance/log` | {offset, size, query_condition:{start_time, end_time}} (configurable via B4H_LOG_BODY) | {logs_data\[\], total_count} |
| GET `/device_maintenance/logFile` | none | full log file |
| POST `/intelli_manager/alg_warehouse/packet_list` | {offset:0, size:100} | {list\[\]} algorithm packages |

device_config also supplies each camera's RTSP URL (cached 5 min, server-side only, never sent to the browser) ↓

## 5a. Live video pipeline

1. Camera/NVR RTSP over TCP (`/channels/201`, or 202 sub-stream if LIVE_SUBSTREAM=1)
2. One OpenCV thread per camera: decode, downscale to 960 px, JPEG quality 70
3. Latest frame shared by all viewers; thread stops after 10 s with no viewer
4. stream/ yields `--frame` parts; snapshot/ returns one frame

## 5b. Movement alarm pipeline

1. One worker thread per enabled rule, same RTSP URL
2. MOG2 background subtraction, masked to the zone polygon
3. score = moving pixels / zone pixels; moving if score ≥ sensitivity
4. After trigger_frames in a row and outside cooldown, save a MovementAlarmEvent (SQLite); user acknowledges it from the page


# B4H Portal

A Django admin portal for the MegCube B4H box. It reads recognition, capture, people, device, log and algorithm data live from the box, shows live camera video, and runs a portal-side movement alarm on RTSP streams.

The local database (SQLite) holds only Django users and sessions, plus movement alarm rules and events. All camera data is read from the box on demand.

## Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Create a `.env` file in the project root:

| Variable | Default | Purpose |
|---|---|---|
| `B4H_BASE_URL` | `https://192.168.90.200` | Box address |
| `B4H_USER` / `B4H_PASS` | `admin` / empty | Box login |
| `DJANGO_SECRET_KEY` | dev key | Change in production |
| `DJANGO_DEBUG` | `1` | Set `0` in production |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated |
| `TIME_ZONE` | `Asia/Dhaka` | Display time zone |
| `RECOG_MAJOR` | `face_basic_business` | Alarm major type |
| `LIVE_SUBSTREAM` | `0` | `1` = use RTSP sub stream (`/channels/NN02`) |
| `B4H_LOG_BODY` | built in | JSON body for the box log query |
| `B4H_LOG_PATH` / `B4H_LOG_FILE_PATH` | `/device_maintenance/log` / `/device_maintenance/logFile` | Log endpoints |
| `LIVE_OPEN_TIMEOUT_MS` / `LIVE_READ_TIMEOUT_MS` | `8000` / `8000` | A dead camera fails after this long instead of hanging a thread |
| `LIVE_FIRST_FRAME_WAIT` | `25` | Seconds a browser tile waits for the first frame (big keyframe intervals are slow) |
| `LIVE_MAX_STREAMS` | `12` | Max simultaneous live tiles. Each holds a server thread; keep it at least 8 below the server's `--threads` |
| `FORECAST_AGENT` | `1` | `0` turns the next-hour forecast agent off |
| `FORECAST_BACKFILL_DAYS` | `28` | History pulled from the box on the agent's first run |
| `FORECAST_MIN_TRAIN_ROWS` | `200` | Below this many training hours only the seasonal average is used |
| `FORECAST_RUN_MINUTE` | `2` | The agent runs this many minutes after each full hour |
| `FORECAST_Z` / `FORECAST_RATIO` / `FORECAST_MIN_DELTA` | `2.0` / `1.5` / `5` | "Investigate" thresholds (see Event forecast agent) |
| `B4H_ALG_PATH` / `B4H_ALG_BODY` | `/intelli_manager/alg_warehouse/packet_list` / `{"offset":0,"size":100}` | Algorithm list |

No box available? Run the fake box: `python mock_box.py`, then start the server with `B4H_BASE_URL=http://127.0.0.1:9001`.

Production (Windows, single process with threads): `waitress-serve --threads=32 config.wsgi:application`. Run one process only, because camera streams and alarm workers live in process memory.

## Project layout

| File | Role |
|---|---|
| `config/urls.py` | Routes (all wrapped in `admin.site.admin_view`, staff only) |
| `boxapp/b4h.py` | Box client: login, session retry, locking |
| `boxapp/box.py` | One shared client per process |
| `boxapp/services.py` | Builds box requests, pagination, filtering |
| `boxapp/mapping.py` | Turns raw box records into table rows |
| `boxapp/admin.py`, `forms.py`, `templates/` | Admin pages and filter forms |
| `boxapp/views.py` | Image proxy, live video, log download, movement alarm routes |
| `boxapp/streams.py` | RTSP to MJPEG (OpenCV, one thread per camera) |
| `boxapp/movement_alarm.py` | Motion detection workers |
| `boxapp/forecast.py` | Next-hour event forecast: collector, models, background agent |
| `boxapp/management/commands/check_live.py` | `manage.py check_live`: finds why a camera does not play |
| `boxapp/models.py` | Page permission models and alarm rule/event tables |
| `mock_box.py` | Fake box for offline development |

## Workflow

### 1. Startup
1. Django loads `config/settings.py` and `.env`.
2. `boxapp/box.py` creates one shared `B4HClient` (one login lock, one call lock).

### 2. Staff login
1. `/` redirects to `/admin/`.
2. The user logs in with a Django staff account (session cookie and CSRF).
3. The sidebar shows only pages the account has `view_*` permission for: Recognition, Captures, People, Devices, Live view, Logs, Algorithms.

### 3. Box authentication (automatic on the first box call)
1. `GET /auth/login/challenge?username=...` returns `session_id`, `salt`, `challenge`.
2. Password hash = `sha256(password + salt + challenge)`.
3. `POST /auth/login` with `{session_id, username, password: hash}`.
4. Every later request sends `Cookie: sessionID=...`.
5. Code 512 (idle session dropped after about 30 s): log in again and retry once.
6. Requests go one at a time, because the box rejects parallel queries.

> Five wrong passwords in a row lock the box account. Check `B4H_PASS` first if login fails.

### 4. Data pages (Recognition, Captures, People, Devices, Logs, Algorithms)
1. The user sets filters and submits (GET, filters in the URL).
2. The form is validated and `services.py` builds the box request: times in milliseconds (seconds for logs), `offset = (page - 1) * size`, size at most 30.
3. `box.call()` returns `data`, or raises `B4HError`, which the page shows as a message.
4. Recognition with "hide deleted" on: fetch the face library, pull up to 5000 matched alarms, drop people no longer in the library, then paginate locally.
5. `mapping.py` converts raw records to rows.
6. Thumbnails load through `/admin/box-image/?uri=...` so the browser never needs the box cookie.

### 5. Live view
1. The page lists cameras from `device_config` and renders one `<img src="/admin/live/<id>/stream/">` per camera.
2. The RTSP URL (which contains the camera password) is looked up server-side and never sent to the browser. It is cached for 5 minutes.
3. A background thread reads RTSP over TCP with OpenCV, downscales to 960 px, encodes JPEG quality 70.
4. All viewers of a camera share that thread. The browser receives MJPEG.
5. The thread stops 10 seconds after the last viewer leaves.

### 6. Movement alarm
1. Open `/admin/movement-alarm/`, pick a camera, enter 3 or more zone points and the thresholds.
2. The page POSTs JSON to `/admin/movement-alarm/create/`. The rule is saved and a worker thread starts.
3. The worker runs MOG2 background subtraction on the RTSP stream, masked to the polygon.
4. `score = moving pixels / zone pixels`. If `score >= sensitivity` for `trigger_frames` frames in a row and the cooldown has passed, a `MovementAlarmEvent` is saved.
5. Events are listed on the page and acknowledged there. Toggle and delete start or stop the worker.

### 7. Errors
- Box unreachable: page shows "B4H box unreachable", check `B4H_BASE_URL`.
- Box error: page shows endpoint, message and code.
- Camera silent: snapshot returns 504, stream ends after about 15 s.

## Portal routes

All routes require staff login.

| Route | Method | Params | Returns |
|---|---|---|---|
| `/admin/boxapp/recognitionrecord/` | GET | `start`, `end`, `minor` (`face_comparison_successful` or `stranger`), `size` (10/20/30), `page`, `hide_deleted` | HTML table |
| `/admin/boxapp/capturerecord/` | GET | `start`, `end`, `target_type` (`all`/`face`/`body`), `size`, `page` | HTML table |
| `/admin/boxapp/person/` | GET | `q` (name or id) | HTML list |
| `/admin/boxapp/device/` | GET | none | HTML list |
| `/admin/boxapp/liveview/` | GET | `cols` (1 to 4) | HTML camera grid |
| `/admin/boxapp/logview/` | GET | `start`, `end`, `size`, `page` | HTML table |
| `/admin/boxapp/algorithmpackage/` | GET | none | HTML table |
| `/admin/box-image/` | GET | `uri` | Image bytes (cached 1 day) |
| `/admin/live/<id>/stream/` | GET | `device_id` | MJPEG (`multipart/x-mixed-replace`) |
| `/admin/live/<id>/snapshot/` | GET | `device_id` | One JPEG, 504 if no frame |
| `/admin/live/<id>/status/` | GET | `device_id` | JSON `{state, error, frame_age_s, size}`: why a tile is blank |
| `/admin/boxapp/eventforecastpage/` | GET | none | HTML: next-hour forecast per camera, flagged hours, forecast vs actual |
| `/admin/forecast/run/` | POST | none | Starts one agent cycle now, redirects back |
| `/admin/forecast/data/` | GET | none | JSON of the latest forecasts |
| `/admin/box-log/download/` | GET | none | Log file attachment |
| `/admin/movement-alarm/` | GET | none | HTML: rules, last 100 events |
| `/admin/movement-alarm/create/` | POST | JSON below, header `X-CSRFToken` | `{ok, id}` or 400 `{error}` |
| `/admin/movement-alarm/<rule_id>/toggle/` | POST | none | Redirect |
| `/admin/movement-alarm/<rule_id>/delete/` | POST | none | Redirect |
| `/admin/movement-alarm/event/<id>/ack/` | POST | none | Redirect |

Date and time filters use `YYYY-MM-DDTHH:MM:SS`. Defaults are the last 24 hours (logs: 7 days).

Create rule body:

```json
{
  "name": "Back door",
  "device_id": 1,
  "zone_points": [[100, 100], [500, 100], [500, 400], [100, 400]],
  "sensitivity": 0.015,
  "trigger_frames": 3,
  "cooldown_seconds": 30,
  "alarm_duration_seconds": 10
}
```

`zone_points` are pixel coordinates on the frame as scaled to 960 px wide.

## B4H box API used

Every JSON reply has the form `{"code": 0, "message": "...", "data": ...}`. Code 0 means success.

| Method and path | Request | Response data |
|---|---|---|
| `GET /auth/login/challenge` | `?username=` | `session_id`, `salt`, `challenge` |
| `POST /auth/login` | `{session_id, username, password}` | `session_id` |
| `POST /device_access/device_config` | `{offset: 0, size: 100}` | List of `{device_id, device_name, rtsp_param.url}` |
| `POST /face_manager/person/query` | `{offset, size <= 30, get_feature: false}` | `{person_list[{person_id, person_info.name}], total_count}` |
| `POST /device_alarm/alarm_history` | `{offset, size, query_condition: {start_time, end_time, alarm_type: [...]}}` | `{list, total_count}` |
| `GET /web/<uri>` or `/device_storage/get_image?image_uri=` | image uri | JPEG bytes |
| `POST /device_maintenance/log` | `{offset, size, query_condition: {start_time, end_time}}` | `{logs_data, total_count}` |
| `GET /device_maintenance/logFile` | none | Log file |
| `POST /intelli_manager/alg_warehouse/packet_list` | `{offset: 0, size: 100}` | `{list}` |

Notes on `alarm_history`:
- `start_time` and `end_time` are epoch milliseconds as strings.
- Recognition uses `{major_type: "face_basic_business", minor_type: ["face_comparison_successful"]}` or `["stranger"]`. Other minor types can crash the box web server, so only use ones listed by `GET /device_alarm/alarm_cap`.
- Captures use `face_capture` and/or `body_capture`, plus a second entry `{major_type: "structure", minor_type: ["face", "pedestrian", "vehicle", "non_motor", "plate"]}`. Without it the box returns code 1073741831 (`not_support`).
- Log times are in seconds.

## Live video: when a camera does not play

Run the diagnostic on the PC that runs the portal. It tests, in order, the box login, the RTSP URL the box
holds, the camera's TCP port, the RTSP open, the first frame and the frame rate, and names the first stage that fails:

```bash
python manage.py check_live          # every camera
python manage.py check_live 1 --substream
```

| Symptom | Cause |
|---|---|
| `cannot reach host:554 ... timed out` | The packets are dropped, not refused: the PC is not on the camera network (Wi-Fi, VPN, VLAN), the NVR is off or has a new DHCP address, or a firewall blocks port 554 |
| Log: `Stream timeout triggered after 8000 ms` | Same as above, seen from the live view: the camera did not answer within `LIVE_OPEN_TIMEOUT_MS` |
| `RTSP open failed` | Wrong camera user/password, wrong stream path, or the camera ran out of RTSP sessions (close other viewers) |
| `opened but no decodable frames` | H.265 / H.264+ stream. Set the sub stream to H.264 and use `LIVE_SUBSTREAM=1` |
| `first frame is slow` | Long keyframe interval on the main stream: use `LIVE_SUBSTREAM=1` |
| Whole portal freezes while video plays | All server threads are busy streaming. Run `waitress-serve --threads=32` (see `LIVE_MAX_STREAMS`) |
| `several OpenCV packages are installed` | `opencv-python` and `-headless` overwrite each other: keep only `opencv-python-headless` |

Several cameras listed by the box can share one IP (for example one Hikvision NVR at `x.x.x.24` with channels
201, 301, 401). If `check_live` fails for all of them with the same host, it is one network or NVR problem, not three.

How to narrow down a timeout, on the portal PC (PowerShell):

```powershell
ipconfig                                    # is the IPv4 address in the camera subnet?
ping <camera-or-nvr-ip>                     # does it answer at all?
Test-NetConnection <camera-or-nvr-ip> -Port 554
```

When port 554 connects, open `rtsp://USER:PASSWORD@IP:554/ISAPI/Streaming/channels/201` in VLC on the same PC. If VLC plays, the portal will too.

Each live tile also shows its own status line, reconnects on its own, and a camera password containing
`@ : / #` is percent-encoded automatically. If the box keeps user and password in separate fields they are added to the URL.

## Event forecast agent (next-hour event count)

The page **Event forecast** predicts, for every camera and event type, how many events to expect in the hour
that is just starting (`next_hour_event_count`) and tells an operator when that is far above normal.
A full description (variables, formulas, worked examples) is in the document *Next-Hour Event Forecast:
Variables, Models and Calculation*.

### Events and series

| `event_type` | Meaning | Source |
|---|---|---|
| `face_comparison_successful` | Face matched a person in the library | Box `alarm_history` |
| `stranger` | Face detected, matched nobody | Box `alarm_history` |
| `face_capture` | A face was captured | Box `alarm_history` |
| `body_capture` | A person (body) was captured | Box `alarm_history` |
| `movement` | Restricted-zone movement alarm | Portal database |

Each event type on each camera is one **series** (for example "Camera 1 + Body capture"). Hours with no event count as 0.

### Variables

One training row is one past hour, using only what was known at the start of that hour:

| Input | Meaning | Example |
|---|---|---|
| `hour` | Hour of day (portal time zone) | 14 |
| `day_of_week` | Monday=0 ... Sunday=6 | 2 (Wednesday) |
| `previous_hour_event_count` | Events in the hour before | 8 |
| `previous_day_event_count` | Events in the same hour yesterday | 10 |
| `event_type` | One of the types above | face recognition |
| `device_id` | Camera | 1 |

**Output / target:** `next_hour_event_count`, the number of events in the hour that starts when the agent runs
(at 14:02 it covers 14:00-15:00). It is never invented: it is counted from the box's timestamped records and from
the portal's own movement alarms. Only completed hours are counted.

### What the agent does every hour (about 2 minutes after the full hour)

The agent is a thread in the server process, started by `config/wsgi.py`.

1. **Collects** new completed hours into `HourlyEventCount` (30 records per request, one-day windows). The first run backfills `FORECAST_BACKFILL_DAYS`. Whole hours are replaced, so running twice never double counts.
2. **Trains and chooses.** Three methods compete:
   - *Seasonal average*: mean for the same camera, type, weekday and hour (falls back to same hour of day, then overall, when fewer than 3 samples). Also defines "normal".
   - *`gbm`*: Poisson gradient boosting (scikit-learn `HistGradientBoostingRegressor`), forced to never decrease when the previous hour or day goes up.
   - *`glm`*: Poisson log-linear model (`PoissonRegressor`): `log(expected) = b0 + series + series x hour + weekday + w1 ln(1 + previous_hour) + w2 ln(1 + previous_day)`.

   The most recent hours (the larger of 48 hours or 20 percent of the history) are held out. Each method predicts them without seeing them, and its score is the mean absolute error `average(|predicted - actual|)`; lowest wins. A model is only used if it beats the seasonal average. Under `FORECAST_MIN_TRAIN_ROWS` training hours, with too little recent data to validate, or if scikit-learn is missing, the seasonal average is the forecast and the page says why.
3. **Predicts** every series that has at least 24 hours of history and an event in the last 14 days, and stores the forecast, method and a recommendation.
4. **Fills in Actual.** After the hour ends, the real count is stored next to the forecast.

### Reading the page

| Column / block | Meaning |
|---|---|
| Expected events | The forecast for the hour that is starting |
| Normal | Average for that camera, type and hour of the week |
| x normal | Expected divided by normal |
| Status | **Investigate** when all three hold: expected >= normal + `FORECAST_Z` x spread, expected >= `FORECAST_RATIO` x normal, and expected - normal >= `FORECAST_MIN_DELTA`. Otherwise **Normal** |
| Actual (past forecasts) | Events that really happened in that hour. Empty until the hour is over. `0` means no events were stored: nothing happened, or the box reported nothing. Only filled when the whole run had no errors, so a box outage does not produce false zeros |
| Cameras without a forecast | Cameras the box lists that are not forecast, with the reason: no events recorded yet, or no event in the last 14 days / less than a day of history |

Predicted far above Actual (for example 18 expected, 0 happened) is worth a look: the camera or NVR may be offline.
The page does not flag "much lower than normal" yet.

Run the first cycle with **Run now**. Quality depends on history: ideally two or more weeks of events per camera;
with a few days the plain average usually wins. Setup: `pip install -r requirements.txt` (adds scikit-learn) and
`python manage.py migrate`. The chosen model is also written to `forecast_model.pkl` (ignored by git).

## Known limits

- Run a single server process. Streams, alarm workers and the forecast agent are in-memory.
- Every open live tile occupies a server thread: start production with `waitress-serve --threads=32 config.wsgi:application`.
- Alarm workers are started when a rule is created or toggled. `MovementAlarmManager.sync()` exists but is not called at startup, so enabled rules do not resume after a restart until toggled.
- Movement detection sees motion only. It does not identify people or vehicles.
- The forecast cannot predict one-off events with no pattern, and no model foresees a surge far larger than anything in the history.
<<<<<<< Updated upstream
- The box connection uses `verify=False` (self-signed certificate). Use it only on a trusted network.
=======
- The box connection uses `verify=False` (self-signed certificate). Use it only on a trusted network.

>>>>>>> Stashed changes
