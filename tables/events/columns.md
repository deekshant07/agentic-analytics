# Columns — events

## Event ID[skip]
- `event_id` | skip | A unique identifier for each event recorded. This column is never null.

## User ID★[PII]
- `user_id` | high | A unique identifier for each user. This column is never null.

## Session ID[skip]
- `session_id` | skip | A unique identifier for each user session. This column is never null.

## Event Name★
- `event_name` | high | The name of the event that occurred, such as 'app_opened' or 'transaction_reconciled'. This column is never null.

## Event Category★
- `event_category` | high | The category of the event, such as 'onboarding' or 'payments'. This column is never null.

## Timestamp
- `timestamp` | medium | The exact time when the event occurred. This column is never null.

## Date
- `date` | medium | The date when the event occurred, formatted as YYYY-MM-DD. This column is never null.

## Hour
- `hour` | low | The hour of the day when the event occurred, in 24-hour format. This column is never null.

## Day of Week
- `day_of_week` | low | The day of the week when the event occurred. This column is never null.

## Week Number
- `week_num` | low | The week number of the year when the event occurred. This column is never null.

## Month
- `month` | low | The month when the event occurred, represented as a number. This column is never null.

## Month Name
- `month_name` | low | The name of the month when the event occurred. This column is never null.

## Platform
- `platform` | medium | The platform on which the event occurred, such as 'android' or 'ios'. This column is never null.

## App Version
- `app_version` | low | The version of the application when the event occurred. This column is never null.

## City
- `city` | medium | The city from which the event was recorded. This column is never null.

## State
- `state` | medium | The state from which the event was recorded. This column is never null.

## Account Type★
- `account_type` | high | The type of account the user has, such as 'basic' or 'pro'. This column is never null.

## Age Bucket
- `age_bucket` | medium | The age range of the user, categorized into buckets. This column is never null.

## Income Bucket
- `income_bucket` | medium | The income range of the user, categorized into buckets. This column is never null.

## Occupation
- `occupation` | medium | The occupation of the user, such as 'student' or 'business_owner'. This column is never null.

## Acquisition Cohort
- `acquisition_cohort` | medium | The source through which the user was acquired, such as 'facebook_ads' or 'organic_search'. This column is never null.

## Install Source
- `install_source` | low | The source from which the app was installed. This column may be null.

## Device Model
- `device_model` | low | The model of the device used to access the app. This column may be null.

## Operating System Version
- `os_version` | low | The version of the operating system on the device. This column may be null.

## Device ID[PII]
- `device_id` | low | A unique identifier for the device. This column may be null.

## Referral Code Used
- `referral_code_used` | low | Indicates whether a referral code was used during installation. This column may be null.

## Country
- `country` | medium | The country from which the event was recorded. This column is never null.

## Language
- `language` | low | The language preference of the user. This column may be null.

## Store
- `store` | low | The store from which the app was downloaded, such as 'app_store' or 'play_store'. This column may be null.

## Is First Open
- `is_first_open` | low | Indicates whether this is the user's first time opening the app. This column may be null.

## Open Source
- `open_source` | low | The source from which the app was opened, such as 'direct' or 'push_notification'. This column may be null.

## Time Since Last Open (Hours)
- `time_since_last_open_hrs` | low | The time in hours since the app was last opened. This column may be null.

## Telecom Operator
- `telecom_operator` | low | The telecom operator used by the user. This column may be null.

## Is Aadhaar Linked[PII]
- `is_aadhaar_linked` | low | Indicates whether the user's Aadhaar is linked. This column may be null.

## Entered Via
- `entered_via` | low | The method used to enter information, such as 'autofill' or 'keyboard'. This column may be null.

## SIM Slot
- `sim_slot` | low | The SIM slot used by the device. This column may be null.

## Binding Method
- `binding_method` | low | The method used for SIM binding. This column may be null.

## SIM State
- `sim_state` | low | The state of the SIM card used. This column may be null.

## Binding Status
- `binding_status` | low | The status of the SIM binding process. This column may be null.

## Duration (Milliseconds)
- `duration_ms` | low | The duration of the event in milliseconds. This column may be null.

## Failure Reason
- `failure_reason` | low | The reason for any failure that occurred during the event. This column may be null.

## OTP Channel
- `otp_channel` | low | The channel through which the OTP was sent, such as 'sms' or 'whatsapp'. This column may be null.

## Attempt Number
- `attempt_number` | low | The number of attempts made for a particular action. This column may be null.

## Time to Verify (Seconds)
- `time_to_verify_sec` | low | The time taken to verify an action in seconds. This column may be null.

## Auto Read SMS
- `auto_read_sms` | low | Indicates whether the app can automatically read SMS. This column may be null.

## PAN Type
- `pan_type` | low | The type of PAN provided by the user. This column may be null.

## NSDL Status
- `nsdl_status` | low | The status of the NSDL verification. This column may be null.

## Name Match Score
- `name_match_score` | low | The score indicating how well the name matches during verification. This column may be null.

## Is PAN Linked to Aadhaar[PII]
- `pan_linked_to_aadhaar` | low | Indicates whether the user's PAN is linked to their Aadhaar. This column may be null.

## Validation Duration (Milliseconds)
- `validation_duration_ms` | low | The duration taken for validation in milliseconds. This column may be null.

## Masked Aadhaar[PII]
- `aadhaar_masked` | low | The masked version of the Aadhaar number. This column may be null.

## UIDAI Fetch Status
- `uidai_fetch_status` | low | The status of the UIDAI fetch operation. This column may be null.

## Fields Prefilled
- `fields_prefilled` | low | Indicates if any fields were prefilled during the process. This column may be null.

## Bureau
- `bureau` | low | The credit bureau used for verification. This column may be null.

## Pull Status
- `pull_status` | low | The status of the data pull operation. This column may be null.

## CIBIL Score
- `cibil_score` | low | The CIBIL score of the user, indicating creditworthiness. This column may be null.

## Score Band
- `score_band` | low | The band in which the CIBIL score falls. This column may be null.

## Is New to Credit
- `is_new_to_credit` | low | Indicates whether the user is new to credit. This column may be null.

## Active Loan Count
- `active_loan_count` | low | The number of active loans held by the user. This column may be null.

## Credit Card Count
- `credit_card_count` | low | The number of credit cards held by the user. This column may be null.

## Pull Duration (Milliseconds)
- `pull_duration_ms` | low | The duration taken for the data pull operation in milliseconds. This column may be null.

## VKYC Provider
- `vkyc_provider` | low | The provider used for video KYC. This column may be null.

## Slot Time Band
- `slot_time_band` | low | The time band for the slot during which the event occurred. This column may be null.

## Queue Wait Time (Minutes)
- `queue_wait_min` | low | The time spent waiting in the queue in minutes. This column may be null.

## Call Duration (Seconds)
- `call_duration_sec` | low | The duration of the call in seconds. This column may be null.

## Agent Language
- `agent_language` | low | The language spoken by the agent during the interaction. This column may be null.

## Liveness Score
- `liveness_score` | low | The score indicating the liveness of the user during verification. This column may be null.

## Reviewer Type
- `reviewer_type` | low | The type of reviewer involved in the process, such as 'agent' or 'ai_assisted'. This column may be null.

## Approval Time (Minutes)
- `approval_time_min` | low | The time taken for approval in minutes. This column may be null.

## MPIN Length
- `mpin_length` | low | The length of the MPIN set by the user. This column may be null.

## Is Biometric Enabled
- `biometric_enabled` | low | Indicates whether biometric authentication is enabled. This column may be null.

## Set Duration (Seconds)
- `set_duration_sec` | low | The duration taken to set the MPIN in seconds. This column may be null.

## Total Duration (Minutes)
- `total_duration_min` | low | The total duration of the event in minutes. This column may be null.

## Steps Completed
- `steps_completed` | low | The number of steps completed during the onboarding process. This column may be null.

## Virtual Debit Card Issued
- `virtual_debit_card_issued` | low | Indicates whether a virtual debit card was issued. This column may be null.

## Account Number Assigned
- `account_number_assigned` | low | Indicates whether an account number was assigned to the user. This column may be null.

## IFSC Code
- `ifsc_code` | low | The IFSC code assigned to the user's bank account. This column may be null.

## Welcome Bonus Credited
- `welcome_bonus_credited` | low | Indicates whether a welcome bonus was credited to the user's account. This column may be null.

## Transaction ID
- `transaction_id` | low | A unique identifier for each transaction. This column may be null.

## UTR
- `utr` | low | The Unique Transaction Reference number for each transaction. This column may be null.

## Transaction Channel
- `transaction_channel` | low | The channel through which the transaction was made, such as 'UPI' or 'NEFT'. This column may be null.

## Payment Instrument
- `payment_instrument` | low | The instrument used for payment, such as 'card' or 'account_number'. This column may be null.

## Transaction Type
- `transaction_type` | low | The type of transaction, either 'CREDIT' or 'DEBIT'. This column may be null.

## Amount
- `amount` | medium | The amount involved in the transaction. This column may be null.

## Currency
- `currency` | low | The currency used for the transaction, such as 'INR'. This column may be null.

## Amount Band
- `amount_band` | low | The band in which the transaction amount falls. This column may be null.

## Source Bank
- `source_bank` | low | The bank from which the transaction originated. This column may be null.

## Source Account Type
- `source_account_type` | low | The type of account from which the transaction was made. This column may be null.

## Source VPA
- `source_vpa` | low | The Virtual Payment Address used for the transaction. This column may be null.

## Beneficiary Bank
- `beneficiary_bank` | low | The bank of the transaction beneficiary. This column may be null.

## Beneficiary VPA
- `beneficiary_vpa` | low | The Virtual Payment Address of the transaction beneficiary. This column may be null.

## Merchant Name
- `merchant_name` | low | The name of the merchant involved in the transaction. This column may be null.

## Merchant Category
- `merchant_category` | low | The category of the merchant involved in the transaction. This column may be null.

## Merchant Category Code
- `merchant_category_code` | low | The code representing the merchant category. This column may be null.

## Transaction Status
- `transaction_status` | medium | The status of the transaction, either 'SUCCESS' or 'FAILED'. This column may be null.

## Settlement Status
- `settlement_status` | low | The status of the transaction settlement. This column may be null.

## Settlement Type
- `settlement_type` | low | The type of settlement for the transaction. This column may be null.

## Value Date
- `value_date` | low | The date on which the transaction value is recognized. This column may be null.

## Network Latency (Milliseconds)
- `network_latency_ms` | low | The latency experienced during the transaction in milliseconds. This column may be null.

## NPCI Error Code
- `npci_error_code` | low | The error code returned by NPCI in case of a failure. This column may be null.

## Failure Bank Side
- `failure_bank_side` | low | Indicates which side of the transaction failed, such as 'beneficiary' or 'payer'. This column may be null.

## Is Retriable
- `is_retriable` | low | Indicates whether the transaction can be retried. This column may be null.

## Jewels Earned
- `jewels_earned` | low | The number of jewels earned through transactions. This column may be null.

## Is First Transaction
- `is_first_transaction` | low | Indicates whether this is the user's first transaction. This column may be null.

## Is Balance Visible
- `balance_visible` | low | Indicates whether the user's balance is visible. This column may be null.

## Notification Count
- `notification_count` | low | The number of notifications received by the user. This column may be null.

## Pending Actions
- `pending_actions` | low | The number of actions pending for the user. This column may be null.

## Notification Type
- `notification_type` | low | The type of notification received, such as 'bill_due' or 'offer'. This column may be null.

## Campaign ID
- `campaign_id` | low | The identifier for the marketing campaign associated with the user. This column may be null.

## Is Tapped
- `is_tapped` | low | Indicates whether the user tapped on a notification. This column may be null.

## Cumulative Jewels
- `cumulative_jewels` | low | The total number of jewels earned by the user over time. This column may be null.

## Can Reschedule
- `can_reschedule` | low | Indicates whether the user can reschedule an action. This column may be null.
