# DISCOVERY: Garmin Connect Personal API

Spike run 2026-11-25 against production (`personal.connectapi.garmin.com`) with one personal key.
Device: fēnix 8 (wrist HR only, no chest strap, no footpod). Raw responses are in `data/raw/`
(gitignored). To reproduce:

```bash
uv run python -I scripts/discover.py          # ~290 requests, ~2.5 min
uv run python -I scripts/decode_fit.py data/raw/activity_download/<day>_<id>/<id>_ACTIVITY.fit
uv run python -I scripts/inventory.py         # field inventory from saved responses
```

## TL;DR

- **28 GET endpoints** in the Swagger spec. The official client wraps 26. The two it skips
  (`/usersummary-service/usersummary/daily` and `/weight-service/weight/all/{s}/{e}`) both work.
- **Per-second data comes only from the FIT file.** No JSON endpoint returns samples, laps, or
  a chart time series for an activity. The FIT has 1 Hz HR, speed, GPS, cadence, power, and four
  of five running-dynamics fields.
- **Rate limit: 100 requests per 60 s fixed window.** It is signalled only by a 429 with a lowercase
  `retry-after` header and a JSON body `{count, limit, retryInSeconds}`. There are no
  `X-RateLimit-*` headers. Rejected requests still increase the count.
- **Not in the API at all:** training readiness, training status, acute/chronic load and load
  focus, race predictions, endurance score, hill score, and recovery time. Nothing in the spec's
  112 schemas covers them.
- **Some Garmin numbers can't be reproduced even from inputs the API does return.** HRV
  `lastNightAvg` is about 3% above the mean of its own 60 readings (and about 3.5% above the median). RHR is not
  the minimum of the 2-min HR series. Activity aggregates (avg HR, avg power, distance) do
  reproduce from the FIT. Our NP is within 0.7% of Garmin's.
- **On a wrist-only setup, the per-second power and running-dynamics samples are themselves
  Garmin model outputs.** They are not direct measurements. "The math behind every number" ends
  at Garmin's sensor algorithms for these channels.

## Claims from the prior read, verified

| Claim | Result |
|---|---|
| Token: `POST …/personal-api-key/token`, `Authorization: ApiKey <key>`, no body | ✅ 200 |
| Response is a raw JWT, expiry from `exp` | ✅ `Content-Type: text/plain`, RS256 JWT, **lifetime 86 400 s (24 h)**. Claims: `client_ecosystem, client_id, client_type, exp, garmin_guid, iat, iss, jti, managed_status, revocation_eligibility, scope` |
| Data calls use `Authorization: Bearer <jwt>` | ✅ |
| Client wraps 26 GET endpoints | ✅ PyPI `garmin-connect-personal-api-python==1.1.0` is byte-identical to `src/` at 958ef58 (still HEAD) |
| Swagger is the source of truth | ✅ The OpenAPI 3.0.1 spec is embedded in the docs page JS bundle. It has 28 paths, all GET. It defines no `securitySchemes` and documents no rate limits or 429 |
| Download returns a ZIP of the original file | ✅ 74 KB ZIP (`PK\x03\x04`) containing one `<id>_ACTIVITY.fit` (176 KB). FIT integrity check passes |

## Rate limits and errors

**Rate limit, as observed:**
- 101st request inside a 60 s window → `429`. Body `{"count":101,"limit":100,"retryInSeconds":12,"rateLimitMetaData":1}`, header `retry-after: 12`.
- The window is **fixed, not sliding**. Both resets fell exactly 60 s after
  the first request of the window.
- The count includes rejected requests. Retrying exactly at `retry-after` gave a second 429 with
  `count: 102` and `retry-after: 0`. **A client must wait `retry-after + 1`** and must not hammer
  the endpoint while limited.
- 40 unpaced requests in 4.35 s gave 40 × 200. No short-burst limiter showed up below the
  100/min limit.
- Responses come through Cloudflare (`Server: cloudflare`, `CF-RAY`, `cf-cache-status: DYNAMIC`).
  All data responses carry `Cache-Control: no-cache, no-store`.
- Not tested: whether the limit is per key, per token, or per IP, and whether a daily quota exists.

**Status codes seen (290 requests):** 200 ×228, 204 ×20, 400 ×32, 429 ×3, 404 ×4, 401 ×2, 500 ×1.

| Probe | Status | Body |
|---|---|---|
| Range longer than the cap | 400 | `{}` (no message) |
| End date before start date | 400 | `{}` |
| Future date range | 200 | `[]` |
| Malformed date in path (`2026-13-45`) | **404** | `{}` |
| `dailySleep` with no `date` | 400 | `{}` |
| Activity ID that doesn't exist | 404 | `{}` |
| Non-numeric activity ID | **500** | `{}` |
| Unknown path / POST to a GET path | 404 | `{"error":"NOT_FOUND","status":404}` |
| Bad `activityType` on search | 400 | `{}` |
| Garbage bearer token | 401 | empty, no `WWW-Authenticate` |
| No `Authorization` header | **400** | `{"error":"Authentication missing","status":400}` |
| Invalid API key on token endpoint | 401 | empty |
| HRV / menstrual on a day with no data | **204** | empty (`Content-Type: application/json`) |

Error bodies carry no message, so a client can't tell "range too long" from "bad parameter"
except by the status code. Empty days come back in three different ways: 204 (HRV, menstrual),
200 with all fields null (daily HR, SpO2, sleep, fitness age), or the day is simply left out of
the list (range endpoints).

## Date-range caps

Probe method: the range always ends today. I stepped through 14, 28, 29, 31, 32, 60, 92, 183,
364–367, 731, and 1826 days, then bisected to the exact boundary. "Days" means **inclusive**
calendar days.

| Endpoint | Documented | Measured max (inclusive days) | First rejected |
|---|---|---|---|
| `stats/stress/daily/{s}/{e}` | 28 | **28** | 29 → 400 |
| `stats/steps/daily/{s}/{e}` | 28 | **28** | 29 → 400 |
| `stats/hydration/daily/{s}/{e}` | 31 | **31** | 32 → 400 |
| `bodyBattery/reports/daily` | 31 | **31** | 32 → 400 |
| `dailySleepSummariesByDate` | 31 | **32** (off by one: the docs mean end−start ≤ 31) | 33 → 400 |
| `weight/all/{s}/{e}` | 364 | **365** (same off-by-one) | 366 → 400 |
| `v2/dailysummaries` | 1 year | **366** | 367 → 400 |
| `hrv/daily/{s}/{e}` | none | **367** | 368 → 400 |
| `maxmet/daily/{s}/{e}` (VO2 max) | none | **367** | 368 → 400 |
| `weightgoal/{s}/{e}` | none | no cap seen at 1826 | — |
| `weight/range/{s}/{e}` | none | no cap seen at 1826 | — |
| `wellnessactivity …/range/{s}/{e}` | none | no cap seen at 1826 | — |
| `activities/search` (date filter) | none | no cap seen at 1826 | — |
| `activities/search` `limit` | default 99 | 200 returned 200. 500/1000/5000 returned all 226 activities, so the cap is above 226 and not found | — |

**Limitation:** wellness data on this account covered only the 2 days before the spike, so long ranges came back
nearly empty. The probes show which ranges are *accepted*. They don't show whether a full year
of dense data is silently truncated. Re-test this on an account with long history.

## Endpoint catalog

`Wrapped` = has a method in the official client. Each tag applies to the metric. See the RAW/DERIVED rules below.

### Activity

**`GET /activitylist-service/activities/search/activities`** (wrapped as `search_activities`)
The spec lists 32 filter params, including `start/limit`, `startDate/endDate` (local start),
`activityType/SubType`, distance, duration, elevation, a bounding box, `minAvgPower`,
`requiredField[]`, `sortBy/sortOrder`, and `excludeChildren`. The client exposes only 6 of them.
Returns a list of activity summaries with ~110 fields each:
- Identity and time: `activityId, activityUUID, activityName, startTimeLocal/GMT, endTimeGMT, beginTimestamp, activityType{typeKey,parentTypeId}, eventType, lapCount, locationName`
- RAW: `distance, duration, elapsedDuration, movingDuration, elevationGain/Loss, min/max/avgElevation, averageSpeed, maxSpeed, start/endLatitude/Longitude, averageHR, maxHR, averageRunningCadenceInStepsPerMinute, maxRunningCadence…, maxDoubleCadence, steps, avgPower, maxPower, normPower, avgVerticalOscillation, avgGroundContactTime, avgStrideLength, avgVerticalRatio, maxVerticalSpeed, hrTimeInZone_1..5, powerTimeInZone_1..5, fastestSplit_1000, fastestSplit_1609, splitSummaries[]`
- DERIVED: `calories, bmrCalories, aerobicTrainingEffect, anaerobicTrainingEffect, trainingEffectLabel, *TrainingEffectMessage, activityTrainingLoad, avgGradeAdjustedSpeed, differenceBodyBattery, moderate/vigorousIntensityMinutes, waterEstimated`
- PII on every item: `ownerFullName`, `ownerDisplayName`, and profile image URLs

**`GET /activity-service/activity/{id}`** (wrapped as `get_activity`)
`summaryDTO` holds the search fields above plus `minHR, minPower, totalWork, averageMovingSpeed`
(RAW) and `beginPotentialStamina, endPotentialStamina, minAvailableStamina, impactLoad` (DERIVED).
`metadataDTO` holds `fileFormat` (`fit`), `isOriginal`, `manufacturer`, `lapCount`,
`hasRunPowerWindData`, `elevationCorrected`, `personalRecord`, and `sensors` (null here).
`splitSummaries[]` gives aggregates per split *type*, not per split. Here the types were
`RWD_RUN` ×6, `RWD_WALK` ×8, `RWD_STAND` ×2, and `INTERVAL_ACTIVE` ×1. Each aggregate carries
pace, HR, cadence, power, GCT, VO, stride, and `avgForce/maxForce/normalizedForce`.
**There are no per-lap, per-split, or per-second data in JSON.** The spec also has
`lactateThresholdBpm/Speed` and `recoveryHeartRate` in `SummaryDTO`. They were absent here, and
LT is probably only filled in when the watch detects a new threshold.

**`GET /activity-service/activity/{id}/workouts`** (wrapped)
Returns `[{index, workoutName, sport, manufacturer, steps[], workoutPhrase}]`. For a free run it
gave `workoutPhrase: TRAINING_HISTORY_REPLACED_BASE` with no steps. That phrase is the
daily-suggested-workout system leaking through. DERIVED.

**`GET /download-service/files/activity/{id}`** (wrapped as `download_activity`)
Returns a ZIP containing the original FIT. See the FIT section.

**`GET /activity-service/activity/activityTypes/simple`** (wrapped)
Returns 155 `{key, parent}` entries. Reference data.

**`GET /wellnessactivity-service/activity/summary/range/{from}/{until}`** (wrapped)
Returned `[]` across 5 years. This is probably for activities recorded by all-day monitoring
rather than started from the watch. Not exercised.

### Daily summaries

**`GET /usersummary-service/v2/dailysummaries?startDate&endDate`** (wrapped). The best bulk endpoint, capped at 366 days.
Returns `{data:[…]}`, one row per day:
- `wellnessChronology{start/end Gmt/Local, totalDurationInMillis}`
- `movement{steps{goal,value,distanceInMeters}, floorsAscended, floorsDescended}`: RAW
- `calories{burnedResting, burnedActive, burnedTotal, consumedGoal, consumedRemaining}`: DERIVED
- `heartRate{minValue, maxValue}` is RAW. `heartRate.restingValue` is DERIVED.
- `intensityMinutes{goal, moderate, vigorous}`: DERIVED
- `stress{avgLevel, maxLevel, *Proportion, *DurationInMillis, qualifier}`: DERIVED
- `bodyBattery{min/max/charged/drained/latestValue, featureVersion, dynamicFeedbackEvent, endOfDay…, activityEvents[]}`: DERIVED
- `respiration{avg/min/max/latestValue, algorithmVersion}`: RAW (optical estimate)
- `pulseOx{avgAltitudeInMeters}`, `hydration{goalInMl, consumedInMl}`: hydration is user-logged

**`GET /usersummary-service/usersummary/daily?calendarDate`** (*not wrapped*)
A flat version of the same data for one day, with about 90 fields. It adds `highlyActiveSeconds,
activeSeconds, sedentarySeconds, sleepingSeconds, measurableAwake/AsleepDuration,
min/maxAvgHeartRate, lastSevenDaysAvgRestingHeartRate, bodyBatteryAtWakeTime,
bodyBatteryDuringSleep, averageMonitoringEnvironmentAltitude, lastSyncTimestampGMT, *Spo2` (null
here), and `abnormalHeartRateAlertsCount`. Activity-level body-battery events link to
`activityId`. Days without data return 200 with nulls. This endpoint duplicates v2 one day at a
time, so prefer v2.

**`GET /usersummary-service/stats/steps/daily/{s}/{e}`** (wrapped, 28-day cap)
Returns `[{calendarDate, totalSteps, totalDistance, stepGoal}]`. RAW.

**`GET /usersummary-service/stats/stress/daily/{s}/{e}`** (wrapped, 28-day cap)
Returns `[{calendarDate, values{overallStressLevel, rest/low/medium/highStressDuration}}]`. DERIVED.
**No intraday stress time series exists anywhere in the API.**

**`GET /usersummary-service/stats/hydration/daily/{s}/{e}`** (wrapped, 31-day cap)
Returned `[]`, since there was no logged hydration. User input.

### Heart rate, HRV, SpO2, VO2 max

**`GET /wellness-service/wellness/dailyHeartRate?date`** (wrapped)
Returns `{calendarDate, start/endTimestamp*, maxHeartRate, minHeartRate, restingHeartRate,
lastSevenDaysAvgRestingHeartRate, heartRateValueDescriptors, heartRateValues[[ts_ms, bpm]]}`.
**Samples are every 120 s** (228 of 229 gaps), and only for the current day. RHR is DERIVED: it
was about 6% above the series' 2-min minimum. Min and max are RAW and match the series.

**`GET /hrv-service/hrv/{date}`** (wrapped; 204 if there is no data)
Returns `hrvSummary{lastNightAvg, lastNight5MinHigh, weeklyAvg, baseline, status, feedbackPhrase}`
plus `hrvReadings[{hrvValue, readingTimeGMT/Local}]`, which are **5-min overnight values**
(60 readings), and sleep start/end. The readings are RAW (5-min RMSSD-style values). There are no
beat-to-beat RR intervals, so we can't recompute them. `lastNightAvg` is **DERIVED in practice**:
it was about 3% above the mean of the readings and about 3.5% above their median. `5MinHigh` = max(readings) exactly ✓.
`baseline`, `weeklyAvg`, and `status` are DERIVED, and were null or `NONE` because there is under
a week of history.

**`GET /hrv-service/hrv/daily/{s}/{e}`** (wrapped, 367-day cap)
Returns `hrvSummaries[]`, the same summary fields with no readings.

**`GET /wellness-service/wellness/daily/spo2acclimation/{date}`** (wrapped)
Returns `averageSpO2, lowestSpO2, latestSpO2, avgSleepSpO2, spO2SingleValues, spO2HourlyAverages`.
All were null here, probably because Pulse Ox isn't enabled on the watch.
`monitoringEnvironmentValues[[ts, level]]` was filled in at 6-min intervals.

**`GET /metrics-service/metrics/maxmet/daily/{s}/{e}?sport[]`** (wrapped, 367-day cap)
Returns `[{generic{calendarDate, vo2MaxPreciseValue, vo2MaxValue, sport, subSport}, cycling}]`.
A running VO2 max came back. DERIVED.

**`GET /biometric-service/heartRateZones/v2?generateDefaults`** (wrapped)
Returns `[{trainingMethod, maxHeartRateUsed, lactateThresholdHeartRateUsed, restingHeartRateUsed,
zone1..5Floor, sport}]`. This is configuration. Here `trainingMethod` = `HR_MAX`, with max HR and LTHR filled in. This endpoint
is **the only place LTHR is exposed**.

### Sleep

**`GET /sleep-service/sleep/dailySleep?date`** (wrapped)
Returns `sleepTimeSeconds, napTimeSeconds, deep/light/rem/awake/unmeasurableSleepSeconds,
sleepStart/End*, awakeCount, averageRespirationValue, lowest/highestRespirationValue,
avgHeartRate, avgSleepStress, sleepScores{overall, totalDuration, stress, awakeCount,
remPercentage, lightPercentage, deepPercentage, restlessness}` (each with `qualifierKey` and
optimal range), plus `sleepScoreFeedback/Insight`, `sleepNeed{baseline, actual,
*Adjustment, recommendedBedtime*}`, `nextSleepNeed`, and `sleepAlignment`.
**There is no hypnogram or per-epoch stage time series.** Only totals are returned. Stages,
scores, and sleep need are all DERIVED. Days without data return 200 with nulls (566 B).

**`GET /sleep-service/sleep/dailySleepSummariesByDate?startDate&endDate`** (wrapped, 32-day cap)
Returns the same object per day, minus a few fields.

### Body Battery

**`GET /wellness-service/wellness/bodyBattery/reports/daily?startDate&endDate&chunks`** (wrapped, 31-day cap)
Returns `[{date, charged, drained, start/endTimestamp*, bodyBatteryValuesArray[[ts, level]],
bodyBatteryDynamicFeedbackEvent, endOfDay…, bodyBatteryActivityEvent[{eventType,
durationInMilliseconds, bodyBatteryImpact, feedbackType}]}]`. The values come at **irregular
change points** (gaps of 15 min to 2.5 h), not a fixed interval. DERIVED throughout.

### Body composition and goals

**`GET /weight-service/weight/range/{s}/{e}?includeAll`** (wrapped; no cap seen)
Returns `dailyWeightSummaries[{summaryDate, numOfWeightEntries, min/maxWeight, latestWeight,
allWeightMetrics[]}]`, `totalAverage`, and `previous/nextDateWeight`. `weight` is RAW (manual or
scale). `bmi` is a formula. `bodyFat, bodyWater, boneMass, muscleMass, visceralFat,
physiqueRating, metabolicAge` were all null with no smart scale. On an Index scale they would be
DERIVED (BIA model).

**`GET /weight-service/weight/all/{s}/{e}`** (*not wrapped*, 365-day cap)
Returns a flat list of every weigh-in with `date, calendarDate, weight, bmi, weightDelta,
timestampGMT`, plus the body-composition fields. It has no `samplePk` or `sourceType`.

**`GET /goal-service/goal/user/effective/weightgoal/{s}/{e}`** (wrapped)
Returns `{userGoals:[], userGoalRanges:null}`. No goal set.

### Profile, nutrition, fitness age, women's health

**`GET /userprofile-service/userprofile/settings`** (wrapped)
Returns `displayName, measurementSystem, timeZone, firstDayOfWeek`, format preferences,
`availableTrainingDays`, and `preferredLongTrainingDays` (null). There is **no age, sex, height,
weight, max HR, or threshold** here. The FIT `user_profile` message has sex, height, and weight
(and the user's full name).

**`GET /nutrition-service/food/logs/{date}`** (wrapped)
Returns `{mealDate, dayStart/EndTime, dailyNutritionGoals{calories, adjustedCalories},
mealDetails[], loggedFoodsWithServingSizes[]}`. Empty here.

**`GET /fitnessage-service/fitnessage/v2/{date}?fullHistoryScan`** (wrapped)
Returns `chronologicalAge, fitnessAge, previousFitnessAge, achievableFitnessAge, lastUpdated,
components{vigorousDaysAvg, vigorousMinutesAvg, rhr, bmi}`. Each component carries
`value, targetValue, potentialAge, priority, stale, staleInputs`. Fitness age is DERIVED, but it
does expose its component inputs, which helps with explanation.

**`GET /periodichealth-service/menstrualcycle/summary/{date}`** and **`…/dayview/{date}`** (both wrapped)
Returned 204 (no data) on the test account. Shapes are known only from the spec.

## Per-second FIT data (one run, activity <activity_id>)

The run was about 30 minutes with walk breaks. There were **1680
`record` messages at 1 Hz** (1676 of 1679 gaps were 1 s; the rest are timer pauses).
Sensors: fēnix 8 GPS, barometer, accelerometer, and wrist HR (`whr`). No external sensors.

| Channel | Status | FIT field | Coverage |
|---|---|---|---|
| Heart rate | ✅ | `heart_rate` | 100 % |
| Pace / speed | ✅ | `enhanced_speed` (m/s) | 100 % |
| Distance | ✅ | `distance` | 100 % |
| GPS | ✅ | `position_lat/long` (semicircles) | 100 % |
| Altitude | ✅ | `enhanced_altitude` (barometric) | 100 % |
| Cadence | ✅ | `cadence` + `fractional_cadence` (spm/2) | 100 % |
| Power | ✅ | `power`, `accumulated_power` | 99.8 % |
| Vertical oscillation | ✅ | `vertical_oscillation` | 98.3 % |
| Vertical ratio | ✅ | `vertical_ratio` | 98.3 % |
| Stride length | ✅ | `step_length` | 98.3 % |
| Ground contact time | ✅ | `stance_time` | 87.0 % |
| **GCT balance** | ❌ | `stance_time_balance` | needs a chest strap or RD pod |
| **Grade** | ❌ | `grade` | not recorded, but computable from altitude and distance |
| **Temperature** | ❌ | `temperature` | not recorded |
| **Respiration rate** | ❌ in standard fields | (`enhanced_respiration_rate`) | see field 143 below |
| **Beat-to-beat RR / HRV** | ❌ | `hrv` message | absent (wrist HR, no strap) |

**Undocumented record fields** (proprietary numbers not in the public FIT profile), matched
against the API:

| Field | Range | Identification |
|---|---|---|
| 137 / 138 | % values, falling over the run | **Potential / available stamina %**. First and last values exactly match API `beginPotentialStamina`, `endPotentialStamina` and `minAvailableStamina` |
| 90 | small integers, starts ~6 min in | Probably **performance condition** (unconfirmed) |
| 143 | plausible running breaths/min | Probably **respiration rate** (brpm). Session fields 215/216 = its max/min (unconfirmed) |
| 136 | tracks the HR range | Tracks HR range. Unknown |
| 107, 135, 140, 145 | — | Unknown |

The **session** message has standard fields `total_training_effect`,
`total_anaerobic_training_effect`, `training_load_peak` (exactly = API `activityTrainingLoad`),
`normalized_power`, `avg_grade_adjusted_speed`, `total_work`, and `metabolic_calories`.
Proprietary field 221 = API `impactLoad` exactly. Fields 205/206/207 = stamina begin/end/min.
The FIT also has `lap` (auto-laps per mile), `split` (17, the run/walk/stand segments), and
`split_summary`. It also has `time_in_zone` for session, laps, and splits (HR and power zone
boundaries plus `functional_threshold_power` and `threshold_heart_rate`), `zones_target`,
`user_profile` (sex, height, weight, **full name**), 35 `device_info`, and ~4.6 k messages of
proprietary types (233, 534, 325, 326, and others).

**Can we reproduce Garmin's numbers from the FIT?**
| Metric | FIT-derived vs Garmin |
|---|---|
| Avg HR | within 0.3 % ✓ |
| Avg power | within 0.3 % ✓ |
| Normalized power (30 s rolling, 4th-power mean) | within 0.7 % (pause handling differs) |
| Distance | identical ✓ |

## Notably absent

Checked against all 28 paths and all 112 component schemas in the spec. None of these appear,
except as feedback enum strings such as `IMPROVING_VO2_MAX_15` and `IMPROVING_LACTATE_THRESHOLD_12`.

| Metric | Status |
|---|---|
| Training readiness | **Absent** |
| Training status (productive/peaking/…) | **Absent** |
| Training load: acute / chronic / ratio / load focus | **Absent**. Only per-activity `activityTrainingLoad` (EPOC peak) exists. Acute and chronic load would have to be rebuilt from it |
| Race predictions (5K/10K/HM/M) | **Absent** |
| Endurance score | **Absent** |
| Hill score | **Absent** |
| Recovery time | **Absent** |
| Lactate threshold | Partial: LTHR in `heartRateZones`. `lactateThresholdSpeed/Bpm` are in the activity schema but were null on this run |
| FTP / running power threshold | Only in the FIT `zones_target`/`time_in_zone` |
| Per-second activity streams in JSON | **Absent**. FIT only |
| Lap/split detail in JSON | **Absent**. Only split-type aggregates. Per-lap data is in the FIT |
| Intraday stress, steps, respiration | **Absent**. Daily aggregates only |
| Sleep hypnogram / stage epochs | **Absent**. Totals only |
| Beat-to-beat RR intervals | **Absent**. 5-min HRV values only |
| Personal records list, gear, courses, workouts library, devices | **Absent** |
| Write / push (webhooks) | None. Read-only and pull-only, as advertised |

## RAW vs DERIVED

**RAW** = a sensor measurement, or deterministic public math over samples we hold.
**DERIVED** = Garmin's model output whose method isn't public. This holds even when we can
approximate it.

One caveat applies everywhere: on a wrist-only setup, **power and running dynamics are
model-estimated per sample** (accelerometer + GPS + barometer). They are tagged RAW because they
arrive as per-second samples we can do transparent math on. The product should still label
them "Garmin wrist estimate" and not treat them as measurements.

| Metric | Tag | Source | Note |
|---|---|---|---|
| HR (per second, in activity) | RAW | FIT | Optical |
| HR (2-min, all day) | RAW | `dailyHeartRate` | |
| Min / max HR (daily) | RAW | dailyHeartRate, summaries | Matches the series |
| Resting HR | DERIVED | summaries, dailyHeartRate | ≠ min of the series. Approximable |
| Pace / speed, distance, GPS track | RAW | FIT, activity | |
| Elevation gain/loss, altitude | RAW | FIT | Barometric. `isElevationCorrected` flag |
| Grade-adjusted pace | DERIVED | activity, FIT session | Garmin's GAP model. Ours would be computable from grade |
| Grade | RAW (computed) | — | From altitude and distance |
| Cadence, steps | RAW | FIT, summaries | |
| Running power (per sample) | RAW* | FIT | *Wrist model estimate |
| Avg / max / normalized power, total work | RAW | computed from FIT | NP formula is public |
| GCT, vertical oscillation, vertical ratio, stride length | RAW* | FIT | *Wrist estimate. 87–98 % coverage |
| GCT balance | — | — | Not available without a strap |
| Time in HR / power zones | RAW | FIT, activity | Given zone boundaries |
| HR zones, max HR used, LTHR | Config | heartRateZones | User-set or Garmin-detected |
| Splits / laps / run-walk segments | RAW | FIT | |
| Calories (activity and daily, BMR) | DERIVED | activity, summaries | |
| Aerobic / anaerobic training effect | DERIVED | activity, FIT session | Firstbeat |
| Activity training load (EPOC) | DERIVED | activity, FIT session | |
| Stamina (potential / available) | DERIVED | activity, FIT fields 137/138 | Per second in FIT |
| Performance condition | DERIVED | FIT field 90 (probable) | |
| Impact load | DERIVED | activity, FIT session 221 | |
| Intensity minutes | DERIVED | summaries, activity | Rule-based on HR zones. Approximable |
| VO2 max | DERIVED | maxmet | |
| Fitness age | DERIVED | fitnessage | Exposes component inputs |
| HRV 5-min readings | RAW | hrv | We can't recompute them (no RR) |
| HRV last-night avg | DERIVED | hrv | ≠ mean or median of readings |
| HRV 5-min high | RAW | hrv | = max(readings) |
| HRV baseline / status / weekly avg | DERIVED | hrv | |
| Stress level / durations | DERIVED | stress, summaries | HRV-based Firstbeat model |
| Body Battery | DERIVED | bodyBattery, summaries | |
| Sleep duration, stages | DERIVED | dailySleep | Stage classification |
| Sleep score, sleep need, alignment | DERIVED | dailySleep | |
| Respiration rate (daily, sleep) | RAW | summaries, dailySleep | Optical estimate |
| SpO2 | RAW | spo2acclimation | Null on this device config |
| Weight | RAW | weight | |
| BMI | RAW (computed) | weight | Formula |
| Body fat, muscle, water, bone, metabolic age | DERIVED | weight | BIA scale model. Null here |
| Floors | RAW | summaries | Barometric |
| Hydration, food | User input | hydration, food logs | |

## Things the spike couldn't settle

1. **Silent truncation on long ranges.** This account has only 2 days of wellness history.
   Re-run the probes on an account with dense multi-year data.
2. **Whether the rate limit is per key, token, or IP, and whether there's a daily quota.** A
   full historical backfill would hit the 100/min ceiling. 5 years of daily data is about 1825
   single-day calls, or about 5 calls through `v2/dailysummaries`.
3. **The activity search `limit` ceiling** above 226. There aren't enough activities here to find it.
4. **Menstrual, nutrition, hydration, smart-scale, and SpO2 payloads** were empty. Their shapes
   are only known from the spec.
5. **The proprietary FIT fields** (90, 136, 143, and others). Identifying them needs more runs
   with known values.

## Notes for whoever builds next

- Use the client for auth and paths, and attach a `requests` response hook as
  `scripts/discover.py` does. The client raises on non-2xx and throws away headers, so without
  the hook a sync loop can't see `retry-after`. It also caches the bearer token on disk
  (`~/.config/garmin/…` by default; here, gitignored `data/raw/_auth/`).
- Back off on 429 for `retry-after + 1` seconds, and pace requests at ≤ 1.6/s.
- Treat a 204, a 200 with all-null fields, and a missing day in a list all as "no data".
- FIT files carry the user's full name and GPS home locations. Keep them out of git (they
  already are: `*.fit`, `*.zip`, `data/raw/`).
