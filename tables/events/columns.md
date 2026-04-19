# Columns — events

## Event ID[skip]
- `event_id` | skip | A unique identifier for each event recorded in the log.

## User ID★[PII]
- `user_id` | high | A unique identifier for each user interacting with the app.

## Session ID[skip]
- `session_id` | skip | A unique identifier for each user session, tracking user activity within a single session.

## Event Name★
- `event_name` | high | The name of the event that occurred, indicating the type of user interaction.

## Event Category
- `event_category` | medium | The category of the event, indicating the broader context of the user interaction.

## Event Timestamp★
- `timestamp` | high | The exact date and time when the event occurred.

## Event Date★
- `date` | high | The date when the event occurred, formatted as YYYY-MM-DD.

## Event Hour
- `hour` | medium | The hour of the day when the event occurred, in 24-hour format.

## Day of the Week
- `day_of_week` | medium | The day of the week when the event occurred.

## Platform
- `platform` | medium | The platform on which the app was accessed (e.g., Android, iOS, Web).

## App Version
- `app_version` | medium | The version of the app that the user is using.

## City[PII]
- `city` | medium | The city where the user is located.

## State[PII]
- `state` | medium | The state where the user is located.

## Account Type
- `account_type` | medium | The type of account the user has with Jupiter Money.

## Age Bucket
- `age_bucket` | medium | The age range of the user.

## Income Bucket
- `income_bucket` | medium | The income range of the user.

## Occupation
- `occupation` | medium | The occupation of the user.

## Install Source
- `install_source` | medium | The source from which the app was installed.

## Referral Code Used
- `referral_code_used` | medium | Indicates whether a referral code was used during installation.

## Is First Open
- `is_first_open` | medium | Indicates if this is the user's first time opening the app.

## Time Since Last Open (Hours)
- `time_since_last_open_hrs` | medium | The time in hours since the user last opened the app.

## Notification Count
- `notification_count` | medium | The number of notifications received by the user.

## Pending Actions
- `pending_actions` | medium | The number of actions pending for the user to complete.
