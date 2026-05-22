# Analytics Catalog
_Jupiter Money · Generated: 2026-04-23 17:09_

**How to use:**
- Refer to events and columns by their **display names** in responses
- Use `raw_name` values in SQL queries
- `[PII]` columns must never appear in results
- `[skip]` columns are technical — never filter or group by them
- ★ = high-priority dimension — use these first for RCA and segmentation

---
## `events` — User Events Log
_event_log · This table stores user interaction events within a mobile application, primarily focused on onboarding and transaction activities._

### Events (27)
- **Aadhaar Number Entered** → `event_name = 'aadhaar_number_entered'` `[funnel]`
  _This event captures when a user enters their Aadhaar number during the onboarding process._
- **Aadhaar OTP Requested** → `event_name = 'aadhaar_otp_requested'` `[funnel]`
  _This event indicates that the user has requested an OTP for Aadhaar verification._
- **Aadhaar OTP Verified** → `event_name = 'aadhaar_otp_verified'` `[funnel, retention]`
  _This event signifies that the user has successfully verified their Aadhaar OTP._
  - `otp_channel` (OTP Channel): The channel through which the OTP was received. — `sms`=Received via SMS, `uidai_sms`=Received via UIDAI SMS, `whatsapp`=Received via WhatsApp
  - `attempt_number` (Attempt Number): The number of attempts made to verify the OTP.
  - `aadhaar_masked` (Masked Aadhaar): The masked version of the Aadhaar number for security.
  - `uidai_fetch_status` (UIDAI Fetch Status): The status of the UIDAI fetch operation. — `failed`=Fetch operation failed, `partial`=Fetch operation partially successful, `success`=Fetch operation successful
  - `fields_prefilled` (Prefilled Fields): Fields that were prefilled during the verification process.
- **App Install** → `event_name = 'app_install'` `[funnel]`
  _This event captures when the user installs the application._
  - `install_source` (Install Source): The source from which the app was installed. — `app_store_seo`=Installed via app store SEO, `employer_salary`=Installed via employer salary program, `facebook_ads`=Installed via Facebook ads, `google_ads`=Installed via Google ads, `influencer`=Installed via influencer promotion
  - `device_model` (Device Model): The model of the device on which the app was installed. — `Chrome 130/Mac`=Chrome browser on Mac, `Chrome 130/Windows`=Chrome browser on Windows, `Firefox 130/Windows`=Firefox browser on Windows, `OnePlus 13`=OnePlus 13 smartphone, `Poco X7 Pro`=Poco X7 Pro smartphone
  - `os_version` (OS Version): The version of the operating system on the device. — `Android 14`=Android version 14, `Android 15`=Android version 15, `iOS 17`=iOS version 17, `iOS 18`=iOS version 18, `web`=Web version
  - `device_id` (Device ID): The unique identifier for the device.
  - `referral_code_used` (Referral Code Used): Indicates if a referral code was used during installation. — `False`=No referral code used, `True`=Referral code used
  - `country` (Country): The country from which the app was installed. — `IN`=India
  - `language` (Language): The language preference set by the user. — `en`=English, `hi`=Hindi, `kn`=Kannada, `mr`=Marathi, `ta`=Tamil
  - `store` (Store): The store from which the app was downloaded. — `app_store`=App Store, `play_store`=Play Store, `web`=Web version
- **App Opened** → `event_name = 'app_opened'` `[retention]`
  _This event captures when the user opens the application._
  - `is_first_open` (Is First Open): Indicates if this is the first time the app is opened. — `False`=Not the first open, `True`=First open
  - `open_source` (Open Source): The source from which the app was opened. — `deeplink`=Opened via a deep link, `direct`=Opened directly, `push_notification`=Opened via push notification, `widget`=Opened via a widget
  - `time_since_last_open_hrs` (Time Since Last Open (hrs)): The time in hours since the app was last opened.
- **Bureau Pull Completed** → `event_name = 'bureau_pull_completed'` `[funnel]`
  _This event indicates that the bureau pull process has been successfully completed._
  - `bureau` (Bureau): The credit bureau from which the data was pulled. — `CIBIL`=CIBIL credit bureau
  - `pull_status` (Pull Status): The status of the bureau pull operation. — `success`=Pull operation was successful
  - `cibil_score` (CIBIL Score): The CIBIL score retrieved from the bureau.
  - `score_band` (Score Band): The band in which the CIBIL score falls. — `600-649`=Score between 600 and 649, `650-699`=Score between 650 and 699, `700-749`=Score between 700 and 749, `750+`=Score above 750, `<600`=Score below 600
  - `is_new_to_credit` (Is New to Credit): Indicates if the user is new to credit. — `False`=Not new to credit, `True`=New to credit
  - `active_loan_count` (Active Loan Count): The number of active loans the user has.
  - `credit_card_count` (Credit Card Count): The number of credit cards the user has.
  - `pull_duration_ms` (Pull Duration (ms)): The duration of the bureau pull operation in milliseconds.
- **Bureau Pull Initiated** → `event_name = 'bureau_pull_initiated'` `[funnel]`
  _This event captures when the bureau pull process is initiated._
- **Date of Birth Entered** → `event_name = 'dob_entered'` `[funnel]`
  _This event captures when the user enters their date of birth during onboarding._
- **Email Entered** → `event_name = 'email_entered'` `[funnel]`
  _This event captures when the user enters their email address during onboarding._
- **Home Screen Viewed** → `event_name = 'home_screen_viewed'`
  _This event captures when the user views the home screen of the app._
  - `balance_visible` (Balance Visible): Indicates if the user's balance is visible on the home screen. — `False`=Balance not visible, `True`=Balance visible
  - `notification_count` (Notification Count): The number of notifications visible to the user.
  - `pending_actions` (Pending Actions): The number of actions pending for the user.
- **Mobile Number Entered** → `event_name = 'mobile_number_entered'` `[funnel]`
  _This event captures when the user enters their mobile number during onboarding._
  - `telecom_operator` (Telecom Operator): The telecom operator of the entered mobile number. — `Airtel`=Airtel telecom operator, `BSNL`=BSNL telecom operator, `Jio`=Jio telecom operator, `Vi`=Vi telecom operator
  - `is_aadhaar_linked` (Is Aadhaar Linked): Indicates if the mobile number is linked to Aadhaar. — `False`=Not linked to Aadhaar, `True`=Linked to Aadhaar
  - `entered_via` (Entered Via): The method used to enter the mobile number. — `autofill`=Entered via autofill, `keyboard`=Entered via keyboard, `paste`=Entered via paste
- **MPIN Set** → `event_name = 'mpin_set'` `[funnel]`
  _This event captures when the user sets their MPIN during onboarding._
  - `mpin_length` (MPIN Length): The length of the MPIN set by the user.
  - `biometric_enabled` (Biometric Enabled): Indicates if biometric authentication is enabled. — `False`=Biometric not enabled, `True`=Biometric enabled
  - `set_duration_sec` (Set Duration (sec)): The duration taken to set the MPIN in seconds.
- **Name Entered** → `event_name = 'name_entered'` `[funnel]`
  _This event captures when the user enters their name during onboarding._
- **Notification Received** → `event_name = 'notification_received'`
  _This event captures when the user receives a notification from the app._
  - `notification_type` (Notification Type): The type of notification received by the user. — `bill_due`=Notification for bill due, `jewels_earned`=Notification for jewels earned, `kyc_nudge`=Notification for KYC nudge, `low_balance`=Notification for low balance, `offer`=Notification for an offer
  - `campaign_id` (Campaign ID): The ID of the campaign associated with the notification.
  - `is_tapped` (Is Tapped): Indicates if the notification was tapped by the user. — `False`=Notification not tapped, `True`=Notification tapped
- **Onboarding Completed** → `event_name = 'onboarding_completed'` `[funnel, retention]`
  _This event indicates that the user has completed the onboarding process._
  - `total_duration_min` (Total Duration (min)): The total duration taken to complete the onboarding process in minutes.
  - `steps_completed` (Steps Completed): The number of steps completed during onboarding.
  - `virtual_debit_card_issued` (Virtual Debit Card Issued): Indicates if a virtual debit card was issued upon completion. — `True`=Virtual debit card issued
  - `account_number_assigned` (Account Number Assigned): Indicates if an account number was assigned upon completion. — `True`=Account number assigned
  - `ifsc_code` (IFSC Code): The IFSC code assigned to the user's account.
  - `welcome_bonus_credited` (Welcome Bonus Credited): Indicates if a welcome bonus was credited upon completion. — `False`=No welcome bonus credited, `True`=Welcome bonus credited
- **OTP Requested** → `event_name = 'otp_requested'` `[funnel]`
  _This event captures when the user requests an OTP for verification._
- **OTP Verified** → `event_name = 'otp_verified'` `[funnel, retention]`
  _This event signifies that the user has successfully verified their OTP._
  - `otp_channel` (OTP Channel): The channel through which the OTP was received. — `sms`=Received via SMS, `uidai_sms`=Received via UIDAI SMS, `whatsapp`=Received via WhatsApp
  - `attempt_number` (Attempt Number): The number of attempts made to verify the OTP.
  - `time_to_verify_sec` (Time to Verify (sec)): The time taken to verify the OTP in seconds.
  - `auto_read_sms` (Auto Read SMS): Indicates if the SMS was auto-read for verification. — `False`=SMS not auto-read, `True`=SMS auto-read
- **PAN Entered** → `event_name = 'pan_entered'` `[funnel]`
  _This event captures when the user enters their PAN during onboarding._
- **PAN Validation Completed** → `event_name = 'pan_validation_completed'` `[funnel]`
  _This event indicates that the PAN validation process has been successfully completed._
  - `pan_type` (PAN Type): The type of PAN submitted by the user. — `individual`=Individual PAN
  - `nsdl_status` (NSDL Status): The status of the PAN validation with NSDL. — `valid`=PAN is valid
  - `name_match_score` (Name Match Score): The score indicating how well the name matches the PAN records.
  - `pan_linked_to_aadhaar` (PAN Linked to Aadhaar): Indicates if the PAN is linked to Aadhaar. — `False`=Not linked to Aadhaar, `True`=Linked to Aadhaar
  - `validation_duration_ms` (Validation Duration (ms)): The duration of the PAN validation operation in milliseconds.
- **PAN Validation Initiated** → `event_name = 'pan_validation_initiated'` `[funnel]`
  _This event captures when the PAN validation process is initiated._
- **SIM Binding Completed** → `event_name = 'sim_binding_completed'` `[funnel]`
  _This event indicates that the SIM binding process has been successfully completed._
  - `sim_slot` (SIM Slot): The SIM slot used for binding. — `SIM1`=SIM slot 1, `SIM2`=SIM slot 2
  - `binding_method` (Binding Method): The method used for SIM binding. — `npci_vmn`=NPCI VMN method
  - `sim_state` (SIM State): The state of the SIM after binding. — `active`=SIM is active
  - `binding_status` (Binding Status): The status of the SIM binding operation. — `failed`=Binding failed, `success`=Binding successful
  - `duration_ms` (Duration (ms)): The duration of the SIM binding operation in milliseconds.
- **SIM Binding Initiated** → `event_name = 'sim_binding_initiated'` `[funnel]`
  _This event captures when the SIM binding process is initiated._
  - `sim_slot` (SIM Slot): The SIM slot used for binding. — `SIM1`=SIM slot 1, `SIM2`=SIM slot 2
  - `binding_method` (Binding Method): The method used for SIM binding. — `npci_vmn`=NPCI VMN method
  - `sim_state` (SIM State): The state of the SIM during binding. — `active`=SIM is active
  - `binding_status` (Binding Status): The status of the SIM binding operation. — `failed`=Binding failed, `success`=Binding successful
  - `failure_reason` (Failure Reason): The reason for failure if the binding fails. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document blurry
- **Transaction Reconciled** → `event_name = 'transaction_reconciled'` `[funnel]`
  _This event indicates that a transaction has been successfully reconciled._
  - `failure_reason` (Failure Reason): The reason for failure if the transaction fails. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document blurry
  - `transaction_id` (Transaction ID): The unique identifier for the transaction.
  - `utr` (UTR): The unique transaction reference number.
  - `transaction_channel` (Transaction Channel): The channel through which the transaction was made. — `IMPS`=IMPS channel, `NACH`=NACH channel, `NEFT`=NEFT channel, `RTGS`=RTGS channel, `UPI`=UPI channel
  - `payment_instrument` (Payment Instrument): The instrument used for the payment. — `account_number`=Account number, `card`=Card, `mandate`=Mandate, `vpa`=VPA
  - `transaction_type` (Transaction Type): The type of transaction (credit or debit). — `CREDIT`=Credit transaction, `DEBIT`=Debit transaction
  - `amount` (Amount): The amount involved in the transaction.
  - `currency` (Currency): The currency of the transaction. — `INR`=Indian Rupee
  - `amount_band` (Amount Band): The band in which the transaction amount falls. — `5L+`=Amount greater than 5 Lakhs, `<500`=Amount less than 500, `<50K`=Amount less than 50,000, `<5K`=Amount less than 5,000, `<5L`=Amount less than 5 Lakhs
  - `source_bank` (Source Bank): The bank from which the transaction originated. — `Axis Bank`=Axis Bank, `Bank of Baroda`=Bank of Baroda, `Canara Bank`=Canara Bank, `Federal Bank`=Federal Bank, `HDFC Bank`=HDFC Bank
  - `source_account_type` (Source Account Type): The type of account from which the transaction was made. — `pro`=Pro account, `salary`=Salary account, `savings`=Savings account
  - `source_vpa` (Source VPA): The VPA from which the transaction was made.
  - `beneficiary_bank` (Beneficiary Bank): The bank of the beneficiary in the transaction. — `Axis Bank`=Axis Bank, `Bank of Baroda`=Bank of Baroda, `Canara Bank`=Canara Bank, `Federal Bank`=Federal Bank, `HDFC Bank`=HDFC Bank
  - `beneficiary_vpa` (Beneficiary VPA): The VPA of the beneficiary in the transaction.
  - `merchant_name` (Merchant Name): The name of the merchant involved in the transaction. — `1mg`=1mg, `Air India`=Air India, `Airtel`=Airtel, `Ajio`=Ajio, `Amazon`=Amazon
  - `merchant_category` (Merchant Category): The category of the merchant involved in the transaction. — `education`=Education, `emi_repayment`=EMI Repayment, `entertainment_ott`=Entertainment (OTT), `food_dining`=Food and Dining, `groceries`=Groceries
  - `merchant_category_code` (Merchant Category Code): The code representing the merchant category.
  - `transaction_status` (Transaction Status): The status of the transaction. — `FAILED`=Transaction failed, `SUCCESS`=Transaction successful
  - `settlement_status` (Settlement Status): The status of the transaction settlement. — `FAILED`=Settlement failed, `PENDING_SETTLEMENT`=Settlement pending, `SETTLED`=Settlement completed
  - `settlement_type` (Settlement Type): The type of settlement for the transaction. — `batch_nach`=Batch NACH settlement, `batch_neft`=Batch NEFT settlement, `real_time`=Real-time settlement
  - `value_date` (Value Date): The date on which the transaction value is effective. — `2026-01-01`=Value date January 1, 2026, `2026-01-02`=Value date January 2, 2026, `2026-01-03`=Value date January 3, 2026, `2026-01-04`=Value date January 4, 2026, `2026-01-05`=Value date January 5, 2026
  - `network_latency_ms` (Network Latency (ms)): The network latency experienced during the transaction in milliseconds.
  - `npci_error_code` (NPCI Error Code): The error code returned by NPCI if the transaction fails. — `BT`=BT error code, `M2`=M2 error code, `U16`=U16 error code, `U30`=U30 error code, `U43`=U43 error code
  - `failure_bank_side` (Failure Bank Side): Indicates which side of the transaction failed. — `beneficiary`=Beneficiary side failure, `npci_infra`=NPCI infrastructure failure, `payer`=Payer side failure, `psp`=PSP side failure
  - `is_retriable` (Is Retriable): Indicates if the transaction can be retried. — `False`=Not retriable, `True`=Retriable
  - `jewels_earned` (Jewels Earned): The number of jewels earned from the transaction.
  - `is_first_transaction` (Is First Transaction): Indicates if this is the user's first transaction. — `False`=Not the first transaction, `True`=First transaction
  - `cumulative_jewels` (Cumulative Jewels): The total number of jewels earned cumulatively.
- **vKYC Call Completed** → `event_name = 'vkyc_call_completed'` `[funnel]`
  _This event indicates that the vKYC call has been successfully completed._
  - `call_duration_sec` (Call Duration (sec)): The duration of the vKYC call in seconds.
  - `agent_language` (Agent Language): The language spoken by the agent during the call. — `english`=English, `hindi`=Hindi, `regional`=Regional language
  - `liveness_score` (Liveness Score): The score indicating the liveness of the user during the call.
  - `reviewer_type` (Reviewer Type): The type of reviewer who conducted the vKYC call. — `agent`=Human agent, `ai_assisted`=AI-assisted review
  - `approval_time_min` (Approval Time (min)): The time taken for approval after the vKYC call in minutes.
- **vKYC Call Started** → `event_name = 'vkyc_call_started'` `[funnel]`
  _This event captures when the vKYC call is initiated._
- **vKYC Failed** → `event_name = 'vkyc_failed'` `[funnel]`
  _This event indicates that the vKYC call has failed._
  - `failure_reason` (Failure Reason): The reason for failure of the vKYC call. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document blurry
  - `attempt_number` (Attempt Number): The number of attempts made for the vKYC call.
  - `can_reschedule` (Can Reschedule): Indicates if the vKYC call can be rescheduled. — `True`=Can be rescheduled
- **vKYC Initiated** → `event_name = 'vkyc_initiated'` `[funnel]`
  _This event captures when the vKYC process is initiated._
  - `attempt_number` (Attempt Number): The number of attempts made for the vKYC process.
  - `vkyc_provider` (vKYC Provider): The provider conducting the vKYC process. — `federal_bank_vcip`=Federal Bank VCIP
  - `slot_time_band` (Slot Time Band): The time band for the vKYC slot. — `afternoon_12_3`=Afternoon slot (12 PM - 3 PM), `evening_5_7`=Evening slot (5 PM - 7 PM), `morning_9_11`=Morning slot (9 AM - 11 AM)
  - `queue_wait_min` (Queue Wait (min)): The time waited in the queue for the vKYC call in minutes.

### Flow candidates
- **Onboarding / Onboarding**: start=none; success=`onboarding_completed`; failure=none
- **Onboarding / Opened**: start=`app_opened`; success=none; failure=none
- **Transaction / Transaction**: start=none; success=`transaction_reconciled`; failure=none
- **Verification / Aadhaar Otp**: start=`aadhaar_otp_requested`; success=`aadhaar_otp_verified`; failure=none
- **Verification / Aadhaar Otp**: start=`aadhaar_otp_requested`; success=`otp_verified`; failure=none
- **Verification / Bureau Pull**: start=`bureau_pull_initiated`; success=`bureau_pull_completed`; failure=none
- **Verification / Bureau Pull**: start=`bureau_pull_initiated`; success=`bureau_pull_completed`; failure=none
- **Verification / Otp**: start=`otp_requested`; success=`otp_verified`; failure=none

### Key dimensions ★
- **Event Name** (`event_name`): The name of the event that occurred, such as 'app_opened' or 'transaction_reconciled'. This column is never null. — `app_opened` = User opened the application, `transaction_reconciled` = User completed a transaction, `app_install` = User installed the application, `onboarding_completed` = User completed the onboarding process
- **Event Category** (`event_category`): The category of the event, such as 'onboarding' or 'payments'. This column is never null. — `onboarding` = Events related to user onboarding, `payments` = Events related to financial transactions, `engagement` = Events related to user engagement
- **Account Type** (`account_type`): The type of account the user has, such as 'basic' or 'pro'. This column is never null. — `basic` = Basic account type with limited features, `pro` = Pro account type with additional features, `salary` = Salary account type, `savings` = Savings account type

### Other useful columns
- **Timestamp** (`timestamp`): The exact time when the event occurred. This column is never null.
- **Date** (`date`): The date when the event occurred, formatted as YYYY-MM-DD. This column is never null.
- **Platform** (`platform`): The platform on which the event occurred, such as 'android' or 'ios'. This column is never null.
- **City** (`city`): The city from which the event was recorded. This column is never null.
- **State** (`state`): The state from which the event was recorded. This column is never null.
- **Age Bucket** (`age_bucket`): The age range of the user, categorized into buckets. This column is never null.
- **Income Bucket** (`income_bucket`): The income range of the user, categorized into buckets. This column is never null.
- **Occupation** (`occupation`): The occupation of the user, such as 'student' or 'business_owner'. This column is never null.
- **Acquisition Cohort** (`acquisition_cohort`): The source through which the user was acquired, such as 'facebook_ads' or 'organic_search'. This column is never null.
- **Country** (`country`): The country from which the event was recorded. This column is never null.
- **Amount** (`amount`): The amount involved in the transaction. This column may be null.
- **Transaction Status** (`transaction_status`): The status of the transaction, either 'SUCCESS' or 'FAILED'. This column may be null.

### PII — never expose in results
- `user_id` (User ID) [PII]
- `device_id` (Device ID) [PII]
- `is_aadhaar_linked` (Is Aadhaar Linked) [PII]
- `pan_linked_to_aadhaar` (Is PAN Linked to Aadhaar) [PII]
- `aadhaar_masked` (Masked Aadhaar) [PII]

### Suggested metrics
- **Daily Active Users (DAU)** `[Retention]`: This metric measures the unique users who perform any meaningful action on a given day, indicating user engagement. A drop in DAU could signal issues with user retention or app functionality.
  `SELECT DATE(timestamp) AS date, COUNT(DISTINCT user_id) AS value FROM "events" WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS')
OR ("event_name" = 'app_opened' AND "account_type" IS NOT NULL) GROUP BY 1 ORDER BY 1`
- **Weekly Active Users (WAU)** `[Retention]`: This metric tracks unique users active in a rolling 7-day window, providing a less noisy view of engagement trends. A decline in WAU may indicate waning user interest or effectiveness of marketing efforts.
  `SELECT DATE(timestamp) AS date, COUNT(DISTINCT user_id) AS value FROM "events" WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS')
OR ("event_name" = 'app_opened' AND "account_type" IS NOT NULL) GROUP BY 1 ORDER BY 1`
- **Monthly Active Users (MAU)** `[Retention]`: This metric counts unique users active in a calendar month, serving as a standard measure of user base size. A low MAU relative to DAU could indicate that users are not finding ongoing value in the app.
  `SELECT DATE(timestamp) AS date, COUNT(DISTINCT user_id) AS value FROM "events" WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS')
OR ("event_name" = 'app_opened' AND "account_type" IS NOT NULL) GROUP BY 1 ORDER BY 1`
- **DAU/MAU Stickiness Ratio** `[Retention]`: This ratio indicates the percentage of monthly users who are active daily, reflecting user habit formation. A low ratio suggests that users may not be finding enough value to return frequently.
  `WITH dau AS (SELECT COUNT(DISTINCT user_id) AS n FROM events WHERE event_name = 'app_opened' AND DATE(timestamp) = CURRENT_DATE), mau AS (SELECT COUNT(DISTINCT user_id) AS n FROM events WHERE event_name = 'app_opened' AND DATE(timestamp) >= DATE_TRUNC('month', CURRENT_DATE)) SELECT ROUND(dau.n * 100.0 / NULLIF(mau.n, 0), 1) AS dau_mau_pct FROM dau, mau`
- **Day 1 Retention** `[Retention]`: This metric measures the percentage of new users who return the day after their first session, indicating the app's initial value proposition. Low retention suggests that users are not finding enough value to return.
  `WITH cohort AS (
  SELECT user_id, MIN(DATE(timestamp)) AS first_date
  FROM "events" WHERE "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
  GROUP BY 1
),
returned AS (
  SELECT DISTINCT c.user_id
  FROM cohort c
  JOIN "events" e ON e.user_id = c.user_id
  WHERE "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
  AND DATE(e.timestamp) = c.first_date + INTERVAL '1 days'
)
SELECT
  COUNT(DISTINCT cohort.user_id)   AS cohort_size,
  COUNT(DISTINCT returned.user_id) AS retained,
  ROUND(
    COUNT(DISTINCT returned.user_id) * 100.0
    / NULLIF(COUNT(DISTINCT cohort.user_id), 0), 1
  ) AS d1_retention_pct
FROM cohort
LEFT JOIN returned ON returned.user_id = cohort.user_id`
- **Day 7 Retention** `[Retention]`: This metric indicates the percentage of new users who return on day 7, serving as a key predictor of long-term retention. A low Day 7 retention rate may signal that users are not finding ongoing value in the app.
  `WITH cohort AS (
  SELECT user_id, MIN(DATE(timestamp)) AS first_date
  FROM "events" WHERE "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
  GROUP BY 1
),
returned AS (
  SELECT DISTINCT c.user_id
  FROM cohort c
  JOIN "events" e ON e.user_id = c.user_id
  WHERE "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
  AND DATE(e.timestamp) = c.first_date + INTERVAL '7 days'
)
SELECT
  COUNT(DISTINCT cohort.user_id)   AS cohort_size,
  COUNT(DISTINCT returned.user_id) AS retained,
  ROUND(
    COUNT(DISTINCT returned.user_id) * 100.0
    / NULLIF(COUNT(DISTINCT cohort.user_id), 0), 1
  ) AS d7_retention_pct
FROM cohort
LEFT JOIN returned ON returned.user_id = cohort.user_id`
- **Day 30 Retention** `[Retention]`: This metric measures the percentage of new users who return on day 30, providing a strong signal of product-market fit. A low Day 30 retention rate indicates that the app may not be meeting user needs effectively.
  `WITH first_seen AS (SELECT user_id, MIN(DATE(timestamp)) AS first_date FROM events GROUP BY 1), returned AS (SELECT DISTINCT f.user_id FROM first_seen f JOIN events e ON e.user_id = f.user_id WHERE DATE(e.timestamp) = f.first_date + INTERVAL '30 days') SELECT COUNT(DISTINCT first_seen.user_id) AS cohort_size, COUNT(DISTINCT returned.user_id) AS retained, ROUND(COUNT(DISTINCT returned.user_id) * 100.0 / NULLIF(COUNT(DISTINCT first_seen.user_id), 0), 1) AS d30_retention_pct FROM first_seen LEFT JOIN returned ON returned.user_id = first_seen.user_id`
- **New Users Acquisition** `[Acquisition]`: This metric tracks the number of users successfully completing onboarding funnel. A decline in new users may indicate funnel inefficiencies or less user acquisition
  `SELECT DATE(timestamp) AS date, COUNT(DISTINCT user_id) AS value FROM "events" WHERE "event_name" = 'onboarding_completed' GROUP BY 1 ORDER BY 1`
- **Activation Rate** `[Activation]`: This metric measures the percentage of new users who complete the key activation event, indicating they have experienced the app's core value. A low activation rate suggests that users are not engaging with the app effectively.
  `-- % of [onboarding_completed] users who also did [transaction_reconciled]
SELECT ROUND(
  COUNT(DISTINCT CASE WHEN "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS' THEN user_id END) * 100.0
  / NULLIF(
      COUNT(DISTINCT CASE WHEN "event_name" = 'onboarding_completed' THEN user_id END), 0),
  1
) AS pct
FROM "events" WHERE "event_name" IN ('transaction_reconciled', 'onboarding_completed')`
- **User Churn Rate** `[Retention]`: This metric indicates the percentage of previously active users who show no activity in the current period, serving as an inverse measure of retention. High churn rates can signal significant issues with user satisfaction or app performance.
  `-- Churn Rate: users active in prev 60-day period who are absent in last 60 days
WITH prev AS (
  SELECT DISTINCT user_id
  FROM "events"
  WHERE DATE(timestamp)
    BETWEEN CURRENT_DATE - INTERVAL '120 days'
        AND CURRENT_DATE - INTERVAL '61 days'
  AND "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
),
curr AS (
  SELECT DISTINCT user_id
  FROM "events"
  WHERE DATE(timestamp) >= CURRENT_DATE - INTERVAL '60 days'
  AND "event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS'
)
SELECT
  COUNT(DISTINCT prev.user_id)  AS prev_period_users,
  COUNT(DISTINCT curr.user_id)  AS curr_period_users,
  ROUND(
    (1 - COUNT(DISTINCT curr.user_id) * 1.0
       / NULLIF(COUNT(DISTINCT prev.user_id), 0)) * 100, 1
  ) AS churn_rate_pct
FROM prev
LEFT JOIN curr ON curr.user_id = prev.user_id`

---
## `users` — User Information
_dimension · This table stores information about users, including their demographics, account details, and engagement metrics._

### Key dimensions ★
- **User ID** (`user_id`): A unique identifier for each user. This column is never null.
- **Signup Date** (`signup_date`): The date and time when the user signed up. This column is never null.
- **KYC Completed** (`kyc_completed`): Indicates whether the user has completed the Know Your Customer process. This column is never null. — `true` = User has completed KYC, `false` = User has not completed KYC

### Other useful columns
- **Platform** (`platform`): The platform through which the user accessed the service, such as Android, iOS, or web. This column is never null.
- **App Version** (`app_version`): The version of the application used by the user. This column is never null.
- **City** (`city`): The city where the user is located. This column is never null.
- **State** (`state`): The state where the user is located. This column is never null.
- **Age Bucket** (`age_bucket`): The age range of the user, categorized into buckets. This column is never null.
- **Income Bucket** (`income_bucket`): The income range of the user, categorized into buckets. This column is never null.
- **Occupation** (`occupation`): The occupation of the user. This column is never null.
- **Acquisition Cohort** (`acquisition_cohort`): The method through which the user was acquired. This column is never null.
- **Account Type** (`account_type`): The type of account the user holds. This column is never null.
- **Primary Bank** (`primary_bank`): The primary bank associated with the user's account. This column is never null.
- **Sessions Per Week** (`sessions_per_week`): The number of sessions the user engages in per week. This column is never null.
- **Average Transaction Amount** (`avg_txn_amount`): The average amount of transactions made by the user. This column is never null.

### Suggested metrics
- **Average Sessions Per User**: Measures the average number of sessions per user, indicating user engagement.
  `SELECT AVG(sessions_per_week) FROM users`
- **KYC Completion Rate**: Measures the percentage of users who have completed the KYC process, important for compliance.
  `SELECT COUNT(*) / (SELECT COUNT(*) FROM users) FROM users WHERE kyc_completed = true`
- **Average Transaction Amount**: Measures the average transaction amount across all users, indicating financial activity.
  `SELECT AVG(avg_txn_amount) FROM users`
- **User Distribution by Platform**: Shows the distribution of users across different platforms, useful for platform-specific strategies.
  `SELECT platform, COUNT(*) FROM users GROUP BY platform`
- **User Distribution by Age Bucket**: Shows the distribution of users across different age groups, useful for targeted marketing.
  `SELECT age_bucket, COUNT(*) FROM users GROUP BY age_bucket`
- **User Distribution by Income Bucket**: Shows the distribution of users across different income levels, useful for financial product offerings.
  `SELECT income_bucket, COUNT(*) FROM users GROUP BY income_bucket`
- **User Acquisition by Cohort**: Measures the number of users acquired through different channels, important for marketing effectiveness.
  `SELECT acquisition_cohort, COUNT(*) FROM users GROUP BY acquisition_cohort`

---
## Custom Events
_Use these names directly — never expand the SQL manually._

### activated_user
_Users who have successfully did a success transaction _
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "is_first_transaction" IS NOT NULL)
```

### active_user
_Onboarded users who have opened the app or did a success transaction_
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS')
OR ("event_name" = 'app_opened' AND "account_type" IS NOT NULL)
```

### transacting_user
_Users who have successfully completed a transaction._
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS')
```

### onboarded_user
_Users who have completed onboarding Journey_
```sql
WHERE ("event_name" = 'onboarding_completed')
```

### in_app_transacting_users
_Users who did s success transaction using jupiter app_
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "transaction_channel" IS NOT NULL AND "transaction_status" = 'SUCCESS')
```

### off_app_transacting_users
_Users who did a success transaction outside jupiter app_
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "transaction_channel" IS NULL AND "transaction_status" = 'SUCCESS')
```

### jupiter_active_user
_Onboarded users who have opened the app or did a success transaction using jupiter app_
```sql
WHERE ("event_name" = 'transaction_reconciled' AND "transaction_status" = 'SUCCESS' AND "transaction_channel" IS NOT NULL)
OR ("event_name" = 'app_opened' AND "account_type" IS NOT NULL)
```

---
## Exclusions — Apply to every single query

```sql
WHERE
  AND user_id NOT LIKE 'test_%'
```

## Business Definitions

_How the business defines composite concepts — use these consistently in all queries._

- **DAU**: Number of unique users who opened the app on a given day.
  `COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'app_opened')`
- **Conversion Rate**: Percentage of users who completed onboarding out of those who started it.
  `(COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'onboarding_completed') * 100.0) / NULLIF(COUNT(DISTINCT user_id) FILTER (WHERE event_name IN ('aadhaar_number_entered', 'aadhaar_otp_requested')), 0)`
- **LTV**: Estimated revenue generated from a user over their lifetime.
  `SUM(amount) FILTER (WHERE event_name = 'transaction_reconciled')`
- **Retention**: Standard retention definition — please curate for this business.

## Conventions

- Default time window: last 7 days unless user specifies otherwise.
- Currency is in INR.
- Date grouping is typically done by day.
