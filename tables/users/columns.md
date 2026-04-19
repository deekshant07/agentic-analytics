# Columns — users

## User ID[skip]
- `user_id` | skip | A unique identifier for each user. This column is never null.

## Signup Date★
- `signup_date` | high | The date and time when the user signed up for the service. This column is never null.

## Platform
- `platform` | medium | The platform through which the user accessed the service, such as Android, iOS, or web. This column is never null.

## App Version
- `app_version` | medium | The version of the app used by the user. This column is never null.

## City
- `city` | medium | The city where the user is located. This column is never null.

## State
- `state` | medium | The state where the user is located. This column is never null.

## Age Bucket
- `age_bucket` | medium | The age range of the user, categorized into buckets. This column is never null.

## Income Bucket
- `income_bucket` | medium | The income range of the user, categorized into buckets. This column is never null.

## Occupation
- `occupation` | medium | The occupation of the user. This column is never null.

## Acquisition Cohort
- `acquisition_cohort` | medium | The marketing channel through which the user was acquired. This column is never null.

## KYC Completed★
- `kyc_completed` | high | Indicates whether the user has completed the Know Your Customer (KYC) process. This column is never null.

## Account Type
- `account_type` | medium | The type of account the user holds with the neobank. This column is never null.

## Primary Bank
- `primary_bank` | medium | The primary bank associated with the user's account. This column is never null.

## Sessions Per Week
- `sessions_per_week` | medium | The number of sessions the user engages in per week. This column is never null.

## Average Transaction Amount★
- `avg_txn_amount` | high | The average amount of transactions made by the user. This column is never null.
