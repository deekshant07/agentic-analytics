# Analytics Catalog
_Jupiter Money · Generated: 2026-04-19 12:05_

**How to use:**
- Refer to events and columns by their **display names** in responses
- Use `raw_name` values in SQL queries
- `[PII]` columns must never appear in results
- `[skip]` columns are technical — never filter or group by them
- ★ = high-priority dimension — use these first for RCA and segmentation

---
## `events` — User Events Log
_event_log · This table stores user interaction events within the Jupiter Money app, tracking user engagement and onboarding processes._

### Events (27)
- **Aadhaar Number Entered** → `event_name = 'aadhaar_number_entered'` `[funnel]`
  _This event captures when a user inputs their Aadhaar number during the onboarding process._
- **Aadhaar OTP Requested** → `event_name = 'aadhaar_otp_requested'` `[funnel]`
  _This event indicates that the user has requested an OTP to verify their Aadhaar number._
- **Aadhaar OTP Verified** → `event_name = 'aadhaar_otp_verified'` `[funnel, retention]`
  _This event signifies that the user has successfully verified their Aadhaar number using the OTP._
  - `otp_channel` (OTP Channel): The channel through which the OTP was received. — `sms`=Received via SMS, `uidai_sms`=Received via UIDAI SMS service, `whatsapp`=Received via WhatsApp
  - `attempt_number` (Attempt Number): The number of attempts made to verify the OTP.
  - `aadhaar_masked` (Masked Aadhaar Number): The masked version of the Aadhaar number entered by the user.
  - `uidai_fetch_status` (UIDAI Fetch Status): The status of the UIDAI fetch operation during OTP verification. — `failed`=The fetch operation failed, `partial`=The fetch operation was partially successful, `success`=The fetch operation was successful
  - `fields_prefilled` (Prefilled Fields): Fields that were prefilled based on the Aadhaar information.
- **App Installed** → `event_name = 'app_install'` `[funnel]`
  _This event captures when the user installs the app on their device._
  - `install_source` (Install Source): The source from which the app was installed. — `app_store_seo`=Installed via app store SEO, `employer_salary`=Installed via employer salary program, `facebook_ads`=Installed via Facebook ads, `google_ads`=Installed via Google ads, `influencer`=Installed via influencer promotion
  - `device_model` (Device Model): The model of the device on which the app was installed. — `Chrome 130/Mac`=Chrome browser on Mac, `Chrome 130/Windows`=Chrome browser on Windows, `Firefox 130/Windows`=Firefox browser on Windows, `OnePlus 13`=OnePlus 13 smartphone, `Poco X7 Pro`=Poco X7 Pro smartphone
  - `os_version` (OS Version): The version of the operating system on the device. — `Android 14`=Android version 14, `Android 15`=Android version 15, `iOS 17`=iOS version 17, `iOS 18`=iOS version 18, `web`=Web version
  - `device_id` (Device ID): The unique identifier for the device.
  - `referral_code_used` (Referral Code Used): Indicates whether a referral code was used during installation. — `False`=No referral code used, `True`=Referral code was used
  - `country` (Country): The country from which the app was installed. — `IN`=India
  - `language` (Language): The preferred language set by the user. — `en`=English, `hi`=Hindi, `kn`=Kannada, `mr`=Marathi, `ta`=Tamil
  - `store` (Store): The store from which the app was downloaded. — `app_store`=Apple App Store, `play_store`=Google Play Store, `web`=Web version
- **App Opened** → `event_name = 'app_opened'` `[retention]`
  _This event captures when the user opens the app._
  - `is_first_open` (Is First Open): Indicates whether this is the first time the app is being opened. — `False`=Not the first open, `True`=This is the first open
  - `open_source` (Open Source): The source from which the app was opened. — `deeplink`=Opened via a deep link, `direct`=Opened directly, `push_notification`=Opened via a push notification, `widget`=Opened via a widget
  - `time_since_last_open_hrs` (Time Since Last Open (hrs)): The time in hours since the app was last opened.
- **Bureau Pull Completed** → `event_name = 'bureau_pull_completed'` `[funnel]`
  _This event indicates that the bureau pull process has been completed successfully._
  - `bureau` (Bureau): The credit bureau from which the data was pulled. — `CIBIL`=CIBIL credit bureau
  - `pull_status` (Pull Status): The status of the bureau pull operation. — `success`=The bureau pull was successful
  - `cibil_score` (CIBIL Score): The CIBIL score retrieved from the bureau.
  - `score_band` (Score Band): The band in which the CIBIL score falls. — `600-649`=Score between 600 and 649, `650-699`=Score between 650 and 699, `700-749`=Score between 700 and 749, `750+`=Score of 750 or above, `<600`=Score below 600
  - `is_new_to_credit` (Is New to Credit): Indicates whether the user is new to credit. — `False`=Not new to credit, `True`=New to credit
  - `active_loan_count` (Active Loan Count): The number of active loans associated with the user.
  - `credit_card_count` (Credit Card Count): The number of credit cards associated with the user.
  - `pull_duration_ms` (Pull Duration (ms)): The duration taken to complete the bureau pull in milliseconds.
- **Bureau Pull Initiated** → `event_name = 'bureau_pull_initiated'` `[funnel]`
  _This event indicates that the bureau pull process has been initiated._
- **Date of Birth Entered** → `event_name = 'dob_entered'` `[funnel]`
  _This event captures when the user inputs their date of birth during onboarding._
- **Email Entered** → `event_name = 'email_entered'` `[funnel]`
  _This event captures when the user inputs their email address during onboarding._
- **Home Screen Viewed** → `event_name = 'home_screen_viewed'` `[engagement]`
  _This event indicates that the user has viewed the home screen of the app._
  - `balance_visible` (Balance Visible): Indicates whether the user's balance is visible on the home screen. — `False`=Balance is not visible, `True`=Balance is visible
  - `notification_count` (Notification Count): The number of notifications visible to the user.
  - `pending_actions` (Pending Actions): The number of actions pending for the user.
- **Mobile Number Entered** → `event_name = 'mobile_number_entered'` `[funnel]`
  _This event captures when the user inputs their mobile number during onboarding._
  - `telecom_operator` (Telecom Operator): The telecom operator associated with the mobile number. — `Airtel`=Airtel telecom operator, `BSNL`=BSNL telecom operator, `Jio`=Jio telecom operator, `Vi`=Vi telecom operator
  - `is_aadhaar_linked` (Is Aadhaar Linked): Indicates whether the mobile number is linked to Aadhaar. — `False`=Not linked to Aadhaar, `True`=Linked to Aadhaar
  - `entered_via` (Entered Via): The method used to enter the mobile number. — `autofill`=Entered via autofill, `keyboard`=Entered via keyboard, `paste`=Entered via paste
- **MPIN Set** → `event_name = 'mpin_set'` `[funnel]`
  _This event captures when the user sets their MPIN for the app._
  - `mpin_length` (MPIN Length): The length of the MPIN set by the user.
  - `biometric_enabled` (Biometric Enabled): Indicates whether biometric authentication is enabled for the MPIN. — `False`=Biometric authentication is not enabled, `True`=Biometric authentication is enabled
  - `set_duration_sec` (Set Duration (sec)): The duration taken to set the MPIN in seconds.
- **Name Entered** → `event_name = 'name_entered'` `[funnel]`
  _This event captures when the user inputs their name during onboarding._
- **Notification Received** → `event_name = 'notification_received'` `[engagement]`
  _This event indicates that the user has received a notification from the app._
  - `notification_type` (Notification Type): The type of notification received by the user. — `bill_due`=Notification for a due bill, `jewels_earned`=Notification for jewels earned, `kyc_nudge`=Notification for KYC completion, `low_balance`=Notification for low balance, `offer`=Notification for an offer
  - `campaign_id` (Campaign ID): The ID of the campaign associated with the notification.
  - `is_tapped` (Is Tapped): Indicates whether the notification was tapped by the user. — `False`=Notification was not tapped, `True`=Notification was tapped
- **Onboarding Completed** → `event_name = 'onboarding_completed'` `[funnel, retention]`
  _This event signifies that the user has completed the onboarding process._
  - `total_duration_min` (Total Duration (min)): The total time taken to complete the onboarding process in minutes.
  - `steps_completed` (Steps Completed): The number of steps completed during onboarding.
  - `virtual_debit_card_issued` (Virtual Debit Card Issued): Indicates whether a virtual debit card was issued upon completion. — `True`=Virtual debit card was issued
  - `account_number_assigned` (Account Number Assigned): Indicates whether an account number was assigned upon completion. — `True`=Account number was assigned
  - `ifsc_code` (IFSC Code): The IFSC code associated with the user's account.
  - `welcome_bonus_credited` (Welcome Bonus Credited): Indicates whether a welcome bonus was credited upon completion. — `False`=Welcome bonus was not credited, `True`=Welcome bonus was credited
- **OTP Requested** → `event_name = 'otp_requested'` `[funnel]`
  _This event indicates that the user has requested an OTP for verification._
- **OTP Verified** → `event_name = 'otp_verified'` `[funnel, retention]`
  _This event signifies that the user has successfully verified the OTP._
  - `otp_channel` (OTP Channel): The channel through which the OTP was received. — `sms`=Received via SMS, `uidai_sms`=Received via UIDAI SMS service, `whatsapp`=Received via WhatsApp
  - `attempt_number` (Attempt Number): The number of attempts made to verify the OTP.
  - `time_to_verify_sec` (Time to Verify (sec)): The time taken to verify the OTP in seconds.
  - `auto_read_sms` (Auto Read SMS): Indicates whether the OTP was automatically read from an SMS. — `False`=OTP was not auto-read, `True`=OTP was auto-read
- **PAN Entered** → `event_name = 'pan_entered'` `[funnel]`
  _This event captures when the user inputs their PAN during onboarding._
- **PAN Validation Completed** → `event_name = 'pan_validation_completed'` `[funnel]`
  _This event indicates that the PAN validation process has been completed successfully._
  - `pan_type` (PAN Type): The type of PAN submitted by the user. — `individual`=Individual PAN
  - `nsdl_status` (NSDL Status): The status of the PAN validation with NSDL. — `valid`=The PAN is valid
  - `name_match_score` (Name Match Score): The score indicating how well the name matches with the PAN records.
  - `pan_linked_to_aadhaar` (PAN Linked to Aadhaar): Indicates whether the PAN is linked to Aadhaar. — `False`=Not linked to Aadhaar, `True`=Linked to Aadhaar
  - `validation_duration_ms` (Validation Duration (ms)): The duration taken to validate the PAN in milliseconds.
- **PAN Validation Initiated** → `event_name = 'pan_validation_initiated'` `[funnel]`
  _This event indicates that the PAN validation process has been initiated._
- **SIM Binding Completed** → `event_name = 'sim_binding_completed'` `[funnel]`
  _This event indicates that the SIM binding process has been completed successfully._
  - `sim_slot` (SIM Slot): The SIM slot used for binding. — `SIM1`=First SIM slot, `SIM2`=Second SIM slot
  - `binding_method` (Binding Method): The method used for SIM binding. — `npci_vmn`=Binding via NPCI VMN
  - `sim_state` (SIM State): The current state of the SIM after binding. — `active`=SIM is active
  - `binding_status` (Binding Status): The status of the SIM binding operation. — `failed`=Binding failed, `success`=Binding successful
  - `duration_ms` (Duration (ms)): The duration taken to complete the SIM binding in milliseconds.
- **SIM Binding Initiated** → `event_name = 'sim_binding_initiated'` `[funnel]`
  _This event indicates that the SIM binding process has been initiated._
  - `sim_slot` (SIM Slot): The SIM slot used for binding. — `SIM1`=First SIM slot, `SIM2`=Second SIM slot
  - `binding_method` (Binding Method): The method used for SIM binding. — `npci_vmn`=Binding via NPCI VMN
  - `sim_state` (SIM State): The current state of the SIM during binding. — `active`=SIM is active
  - `binding_status` (Binding Status): The status of the SIM binding operation. — `failed`=Binding failed, `success`=Binding successful
  - `failure_reason` (Failure Reason): The reason for the failure of the SIM binding operation, if applicable. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document was blurry
- **Transaction Reconciled** → `event_name = 'transaction_reconciled'` `[funnel]`
  _This event indicates that a transaction has been reconciled successfully._
  - `failure_reason` (Failure Reason): The reason for the failure of the transaction, if applicable. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document was blurry
  - `transaction_id` (Transaction ID): The unique identifier for the transaction.
  - `utr` (UTR): The Unique Transaction Reference number for the transaction.
  - `transaction_channel` (Transaction Channel): The channel through which the transaction was made. — `IMPS`=Immediate Payment Service, `NACH`=National Automated Clearing House, `NEFT`=National Electronic Funds Transfer, `RTGS`=Real Time Gross Settlement, `UPI`=Unified Payments Interface
  - `payment_instrument` (Payment Instrument): The instrument used for the payment. — `account_number`=Payment made using account number, `card`=Payment made using card, `mandate`=Payment made using mandate, `vpa`=Payment made using VPA
  - `transaction_type` (Transaction Type): The type of transaction (credit or debit). — `CREDIT`=Credit transaction, `DEBIT`=Debit transaction
  - `amount` (Amount): The amount involved in the transaction.
  - `currency` (Currency): The currency used for the transaction. — `INR`=Indian Rupee
  - `amount_band` (Amount Band): The band in which the transaction amount falls. — `5L+`=Amount greater than 5 Lakhs, `<500`=Amount less than 500, `<50K`=Amount less than 50,000, `<5K`=Amount less than 5,000, `<5L`=Amount less than 5 Lakhs
  - `source_bank` (Source Bank): The bank from which the transaction originated. — `Axis Bank`=Axis Bank, `Bank of Baroda`=Bank of Baroda, `Canara Bank`=Canara Bank, `Federal Bank`=Federal Bank, `HDFC Bank`=HDFC Bank
  - `source_account_type` (Source Account Type): The type of account from which the transaction was made. — `pro`=Professional account, `salary`=Salary account, `savings`=Savings account
  - `source_vpa` (Source VPA): The VPA from which the transaction was made.
  - `beneficiary_bank` (Beneficiary Bank): The bank of the beneficiary in the transaction. — `Axis Bank`=Axis Bank, `Bank of Baroda`=Bank of Baroda, `Canara Bank`=Canara Bank, `Federal Bank`=Federal Bank, `HDFC Bank`=HDFC Bank
  - `beneficiary_vpa` (Beneficiary VPA): The VPA of the beneficiary in the transaction.
  - `merchant_name` (Merchant Name): The name of the merchant involved in the transaction. — `1mg`=1mg, `Air India`=Air India, `Airtel`=Airtel, `Ajio`=Ajio, `Amazon`=Amazon
  - `merchant_category` (Merchant Category): The category of the merchant involved in the transaction. — `education`=Education, `emi_repayment`=EMI Repayment, `entertainment_ott`=Entertainment (OTT), `food_dining`=Food and Dining, `groceries`=Groceries
  - `merchant_category_code` (Merchant Category Code): The category code of the merchant involved in the transaction.
  - `transaction_status` (Transaction Status): The status of the transaction. — `FAILED`=Transaction failed, `SUCCESS`=Transaction successful
  - `settlement_status` (Settlement Status): The status of the transaction settlement. — `FAILED`=Settlement failed, `PENDING_SETTLEMENT`=Settlement pending, `SETTLED`=Settlement completed
  - `settlement_type` (Settlement Type): The type of settlement for the transaction. — `batch_nach`=Batch NACH settlement, `batch_neft`=Batch NEFT settlement, `real_time`=Real-time settlement
  - `value_date` (Value Date): The date on which the transaction value is effective. — `2026-01-01`=Value date of January 1, 2026, `2026-01-02`=Value date of January 2, 2026, `2026-01-03`=Value date of January 3, 2026, `2026-01-04`=Value date of January 4, 2026, `2026-01-05`=Value date of January 5, 2026
  - `network_latency_ms` (Network Latency (ms)): The network latency experienced during the transaction in milliseconds.
  - `npci_error_code` (NPCI Error Code): The error code returned by NPCI in case of a failure. — `BT`=Error code BT, `M2`=Error code M2, `U16`=Error code U16, `U30`=Error code U30, `U43`=Error code U43
  - `failure_bank_side` (Failure Bank Side): Indicates which side of the transaction failed. — `beneficiary`=Failure on the beneficiary side, `npci_infra`=Failure on NPCI infrastructure, `payer`=Failure on the payer side, `psp`=Failure on the payment service provider side
  - `is_retriable` (Is Retriable): Indicates whether the transaction can be retried. — `False`=Transaction cannot be retried, `True`=Transaction can be retried
  - `jewels_earned` (Jewels Earned): The number of jewels earned from the transaction.
  - `is_first_transaction` (Is First Transaction): Indicates whether this is the user's first transaction. — `False`=Not the first transaction, `True`=This is the first transaction
  - `cumulative_jewels` (Cumulative Jewels): The total number of jewels accumulated by the user.
- **vKYC Call Completed** → `event_name = 'vkyc_call_completed'` `[funnel]`
  _This event indicates that the vKYC call has been completed successfully._
  - `call_duration_sec` (Call Duration (sec)): The duration of the vKYC call in seconds.
  - `agent_language` (Agent Language): The language spoken by the agent during the vKYC call. — `english`=English, `hindi`=Hindi, `regional`=Regional language
  - `liveness_score` (Liveness Score): The score indicating the liveness of the user during the vKYC call.
  - `reviewer_type` (Reviewer Type): The type of reviewer who conducted the vKYC call. — `agent`=Human agent, `ai_assisted`=AI-assisted review
  - `approval_time_min` (Approval Time (min)): The time taken for approval during the vKYC call in minutes.
- **vKYC Call Started** → `event_name = 'vkyc_call_started'` `[funnel]`
  _This event indicates that the vKYC call has been initiated._
- **vKYC Failed** → `event_name = 'vkyc_failed'` `[funnel, rca]`
  _This event indicates that the vKYC call has failed._
  - `failure_reason` (Failure Reason): The reason for the failure of the vKYC call. — `aadhaar_mismatch`=Aadhaar mismatch, `agent_disconnect`=Agent disconnected, `bank_maintenance_window`=Bank maintenance window, `beneficiary_bank_unavailable`=Beneficiary bank unavailable, `blurry_document`=Document was blurry
  - `attempt_number` (Attempt Number): The number of attempts made for the vKYC call.
  - `can_reschedule` (Can Reschedule): Indicates whether the vKYC call can be rescheduled. — `True`=Call can be rescheduled
- **vKYC Initiated** → `event_name = 'vkyc_initiated'` `[funnel]`
  _This event indicates that the vKYC process has been initiated._
  - `attempt_number` (Attempt Number): The number of attempts made for the vKYC process.
  - `vkyc_provider` (vKYC Provider): The provider facilitating the vKYC process. — `federal_bank_vcip`=Federal Bank VCIP
  - `slot_time_band` (Slot Time Band): The time band for the vKYC slot. — `afternoon_12_3`=Afternoon slot from 12 PM to 3 PM, `evening_5_7`=Evening slot from 5 PM to 7 PM, `morning_9_11`=Morning slot from 9 AM to 11 AM
  - `queue_wait_min` (Queue Wait (min)): The time the user has to wait in the queue for the vKYC call in minutes.

### Key dimensions ★
- **Event Name** (`event_name`): The name of the event that occurred, indicating the type of user interaction. — `app_opened` = User opened the app, `home_screen_viewed` = User viewed the home screen, `transaction_reconciled` = User completed a transaction, `notification_received` = User received a notification, `app_install` = User installed the app, `mobile_number_entered` = User entered their mobile number
- **Event Timestamp** (`timestamp`): The exact date and time when the event occurred.
- **Event Date** (`date`): The date when the event occurred, formatted as YYYY-MM-DD.

### Other useful columns
- **Event Category** (`event_category`): The category of the event, indicating the broader context of the user interaction.
- **Event Hour** (`hour`): The hour of the day when the event occurred, in 24-hour format.
- **Day of the Week** (`day_of_week`): The day of the week when the event occurred.
- **Platform** (`platform`): The platform on which the app was accessed (e.g., Android, iOS, Web).
- **App Version** (`app_version`): The version of the app that the user is using.
- **Account Type** (`account_type`): The type of account the user has with Jupiter Money.
- **Age Bucket** (`age_bucket`): The age range of the user.
- **Income Bucket** (`income_bucket`): The income range of the user.
- **Occupation** (`occupation`): The occupation of the user.
- **Install Source** (`install_source`): The source from which the app was installed.
- **Referral Code Used** (`referral_code_used`): Indicates whether a referral code was used during installation.
- **Is First Open** (`is_first_open`): Indicates if this is the user's first time opening the app.
- **Time Since Last Open (Hours)** (`time_since_last_open_hrs`): The time in hours since the user last opened the app.
- **Notification Count** (`notification_count`): The number of notifications received by the user.
- **Pending Actions** (`pending_actions`): The number of actions pending for the user to complete.

### PII — never expose in results
- `user_id` (User ID) [PII]
- `city` (City) [PII]
- `state` (State) [PII]

### Suggested metrics
- **Average Time Since Last Open**: This metric measures the average time users take to return to the app after their last session, indicating user engagement and retention.
  `AVG(time_since_last_open_hrs) WHERE event_name='app_opened'`
- **First Open Rate**: This metric indicates the percentage of users who are opening the app for the first time, which is crucial for understanding onboarding success.
  `COUNT(DISTINCT user_id WHERE is_first_open=true) / NULLIF(COUNT(DISTINCT user_id WHERE event_name='app_install'),0)`
- **Referral Code Usage Rate**: This metric shows the percentage of new users who used a referral code during installation, reflecting the effectiveness of referral marketing.
  `COUNT(DISTINCT user_id WHERE referral_code_used=true) / NULLIF(COUNT(DISTINCT user_id WHERE event_name='app_install'),0)`
- **App Open Rate**: This metric measures the frequency with which users open the app, indicating overall user engagement.
  `COUNT(DISTINCT user_id WHERE event_name='app_opened') / NULLIF(COUNT(DISTINCT user_id WHERE event_name='app_install'),0)`
- **Transaction Completion Rate**: This metric indicates the percentage of initiated transactions that are successfully completed, which is vital for assessing the transaction process.
  `COUNT(DISTINCT user_id WHERE event_name='transaction_reconciled') / NULLIF(COUNT(DISTINCT user_id WHERE event_name='transaction_initiated'),0)`
- **Average Notifications Received**: This metric measures the average number of notifications received by users, which can help assess the effectiveness of communication strategies.
  `AVG(notification_count) WHERE event_name='notification_received'`
- **Pending Actions Rate**: This metric shows the average number of pending actions per user, indicating potential friction points in the user experience.
  `AVG(pending_actions) WHERE event_name='home_screen_viewed'`

---
## `users` — User Information
_dimension · This table stores information about users of the Jupiter Money neobank, including demographics, account details, and engagement metrics._

### Key dimensions ★
- **Signup Date** (`signup_date`): The date and time when the user signed up for the service. This column is never null.
- **KYC Completed** (`kyc_completed`): Indicates whether the user has completed the Know Your Customer (KYC) process. This column is never null.
- **Average Transaction Amount** (`avg_txn_amount`): The average amount of transactions made by the user. This column is never null.

### Other useful columns
- **Platform** (`platform`): The platform through which the user accessed the service, such as Android, iOS, or web. This column is never null.
- **App Version** (`app_version`): The version of the app used by the user. This column is never null.
- **City** (`city`): The city where the user is located. This column is never null.
- **State** (`state`): The state where the user is located. This column is never null.
- **Age Bucket** (`age_bucket`): The age range of the user, categorized into buckets. This column is never null.
- **Income Bucket** (`income_bucket`): The income range of the user, categorized into buckets. This column is never null.
- **Occupation** (`occupation`): The occupation of the user. This column is never null.
- **Acquisition Cohort** (`acquisition_cohort`): The marketing channel through which the user was acquired. This column is never null.
- **Account Type** (`account_type`): The type of account the user holds with the neobank. This column is never null.
- **Primary Bank** (`primary_bank`): The primary bank associated with the user's account. This column is never null.
- **Sessions Per Week** (`sessions_per_week`): The number of sessions the user engages in per week. This column is never null.

### Suggested metrics
- **Average Transaction Amount Per User**: This metric measures the average transaction amount for each user, providing insights into user spending behavior.
  `SELECT AVG(avg_txn_amount) FROM users`
- **KYC Completion Rate**: This metric indicates the percentage of users who have completed the KYC process, which is crucial for regulatory compliance.
  `SELECT COUNT(*) / NULLIF(COUNT(user_id), 0) FROM users WHERE kyc_completed = true`
- **Sessions Per User**: This metric measures the average number of sessions per user, indicating user engagement with the platform.
  `SELECT AVG(sessions_per_week) FROM users`
- **User Distribution by Age Bucket**: This metric shows the distribution of users across different age buckets, helping to understand the demographic profile.
  `SELECT age_bucket, COUNT(*) FROM users GROUP BY age_bucket`
- **User Distribution by Income Bucket**: This metric shows the distribution of users across different income buckets, providing insights into the economic demographics of users.
  `SELECT income_bucket, COUNT(*) FROM users GROUP BY income_bucket`

---
## Custom Events
_Use these names directly — never expand the SQL manually._

### activated_user
_Users who have completed the onboarding process and verified their identity._
```sql
WHERE event_name IN ('onboarding_completed') AND kyc_completed = 'True'
```

### active_user
_Users who have opened the app at least once within the specified time window._
```sql
WHERE event_name IN ('app_opened')
```

### paying_user
_Users who have successfully completed a transaction._
```sql
WHERE event_name IN ('transaction_reconciled') AND transaction_status = 'SUCCESS'
```

### churned_user
_Users who have not opened the app in the last 30 days._
```sql
WHERE event_name IN ('app_opened') AND date < DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)
```

### referral_user
_Users who installed the app using a referral code._
```sql
WHERE event_name IN ('app_install') AND referral_code_used = 'True'
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
  `COUNT(DISTINCT user_id) WHERE event_name = 'app_opened' GROUP BY DATE(timestamp)`
- **MAU**: Number of unique users who opened the app in the last 30 days.
  `COUNT(DISTINCT user_id) WHERE event_name = 'app_opened' AND date >= DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)`
- **Conversion Rate**: Percentage of users who completed the onboarding process out of those who installed the app.
  `(COUNT(DISTINCT user_id) WHERE event_name = 'onboarding_completed') / (COUNT(DISTINCT user_id) WHERE event_name = 'app_install') * 100`
- **LTV**: Estimated revenue generated from a user over their lifetime.
  `SUM(amount) WHERE event_name = 'transaction_reconciled' AND transaction_status = 'SUCCESS' GROUP BY user_id`
- **Churn Rate**: Percentage of users who have not engaged with the app in the last 30 days.
  `(COUNT(DISTINCT user_id) WHERE event_name = 'app_opened' AND date < DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)) / (COUNT(DISTINCT user_id) WHERE event_name = 'app_install') * 100`

## Conventions

- Default time window: last 7 days unless user specifies otherwise.
- Currency is in INR.
- Date grouping is done by day.
