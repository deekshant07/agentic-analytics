"""
seed_events.py — Extend jupiter.duckdb with synthetic events for Apr–Jun 2026.

Run once:
    python qa/seed_events.py

Idempotent: skips if Apr–Jun data already exists.
"""
from __future__ import annotations

import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "jupiter.duckdb"

# ── Distributions sampled from existing Jan–Mar data ──────────────────────────

_EVENT_WEIGHTS = {
    "app_opened":               217388,
    "home_screen_viewed":       203030,
    "transaction_reconciled":   183520,
    "notification_received":     71232,
    "onboarding_completed":       5825,
}

_PLATFORMS      = [("android", 0.61), ("ios", 0.32), ("web", 0.07)]
_CITIES         = ["Mumbai","Delhi","Bangalore","Hyderabad","Pune","Chennai","Kolkata","Ahmedabad","Kochi","Jaipur"]
_STATES         = ["Maharashtra","Delhi","Karnataka","Telangana","Gujarat","Tamil Nadu","West Bengal","Kerala","Rajasthan","Madhya Pradesh"]
_ACCOUNT_TYPES  = [("savings", 0.43), ("salary", 0.25), ("pro", 0.21), ("basic", 0.11)]
_AGE_BUCKETS    = ["23-27","28-32","33-37","18-22","38-45","46+"]
_INCOME_BUCKETS = ["6-10L","10-20L","3-6L","20L+","<3L"]
_OCCUPATIONS    = ["salaried_private","self_employed","salaried_govt","student","freelancer","business_owner"]
_CHANNELS       = [("referral_friend",0.27),("google_ads",0.18),("organic_search",0.14),
                   ("facebook_ads",0.14),("influencer",0.10),("app_store_seo",0.08),("employer_salary",0.08)]
_TXN_STATUSES   = [("SUCCESS", 0.93), ("FAILED", 0.07)]
_TXN_CHANNELS   = [("UPI",0.57),("IMPS",0.15),("NEFT",0.11),("debit_card",0.07),("NACH",0.07),("RTGS",0.03)]


def _pick(weighted: list[tuple[str, float]]) -> str:
    names, weights = zip(*weighted)
    return random.choices(names, weights=weights, k=1)[0]


def _rand_ts(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def _make_row(user_id: str, event_name: str, ts: datetime) -> dict:
    platform = _pick(_PLATFORMS)
    city_idx  = random.randint(0, len(_CITIES) - 1)
    row: dict = {
        "event_id":          str(uuid.uuid4()),
        "user_id":           user_id,
        "session_id":        str(uuid.uuid4())[:16],
        "event_name":        event_name,
        "event_category":    "engagement",
        "timestamp":         ts,
        "date":              ts.strftime("%Y-%m-%d"),
        "hour":              ts.hour,
        "day_of_week":       ts.strftime("%A"),
        "week_num":          int(ts.strftime("%W")),
        "month":             ts.month,
        "month_name":        ts.strftime("%B"),
        "platform":          platform,
        "app_version":       f"3.{random.randint(0,5)}.{random.randint(0,9)}",
        "city":              _CITIES[city_idx],
        "state":             _STATES[city_idx] if city_idx < len(_STATES) else "Other",
        "account_type":      _pick(_ACCOUNT_TYPES),
        "age_bucket":        random.choice(_AGE_BUCKETS),
        "income_bucket":     random.choice(_INCOME_BUCKETS),
        "occupation":        random.choice(_OCCUPATIONS),
        "acquisition_cohort": _pick(_CHANNELS),
        "install_source":    "organic",
        "device_model":      random.choice(["Pixel 7","iPhone 14","Galaxy S23","Redmi Note 12"]),
        "os_version":        random.choice(["Android 13","iOS 17","Android 12","iOS 16"]),
        "device_id":         str(uuid.uuid4())[:16],
        "referral_code_used": random.random() < 0.15,
        "country":           "India",
        "language":          random.choice(["hi","en","ta","te","kn","mr"]),
        "store":             "play_store" if platform == "android" else ("app_store" if platform == "ios" else "web"),
        "is_first_open":     False,
        "open_source":       random.choice(["direct","notification","deeplink"]),
        "time_since_last_open_hrs": round(random.uniform(0, 72), 1),
        "telecom_operator":  random.choice(["Jio","Airtel","Vi","BSNL"]),
        "is_aadhaar_linked": random.random() < 0.85,
        "entered_via":       None,
        "sim_slot":          None,
        "binding_method":    None,
        "sim_state":         None,
        "binding_status":    None,
        "duration_ms":       round(random.uniform(200, 5000), 0) if event_name != "app_opened" else None,
        "failure_reason":    None,
        "otp_channel":       None,
        "attempt_number":    None,
        "time_to_verify_sec": None,
        "auto_read_sms":     None,
        "pan_type":          None,
        "nsdl_status":       None,
        "name_match_score":  None,
        "pan_linked_to_aadhaar": None,
        "validation_duration_ms": None,
        "aadhaar_masked":    None,
        "uidai_fetch_status": None,
        "fields_prefilled":  None,
        "bureau":            None,
        "pull_status":       None,
        "cibil_score":       None,
        "score_band":        None,
        "is_new_to_credit":  None,
        "active_loan_count": None,
        "credit_card_count": None,
        "pull_duration_ms":  None,
        "vkyc_provider":     None,
        "slot_time_band":    None,
        "queue_wait_min":    None,
        "call_duration_sec": None,
        "agent_language":    None,
        "liveness_score":    None,
        "reviewer_type":     None,
        "approval_time_min": None,
        "mpin_length":       None,
        "biometric_enabled": None,
        "set_duration_sec":  None,
        "total_duration_min": None,
        "steps_completed":   None,
        "virtual_debit_card_issued": None,
        "account_number_assigned": None,
        "ifsc_code":         None,
        "welcome_bonus_credited": None,
        "transaction_id":    str(uuid.uuid4()) if event_name == "transaction_reconciled" else None,
        "utr":               str(uuid.uuid4())[:12] if event_name == "transaction_reconciled" else None,
        "transaction_channel": _pick(_TXN_CHANNELS) if event_name == "transaction_reconciled" else None,
        "payment_instrument": None,
        "transaction_type":  random.choice(["P2P","P2M","bill_payment"]) if event_name == "transaction_reconciled" else None,
        "amount":            round(random.uniform(50, 50000), 2) if event_name == "transaction_reconciled" else None,
        "currency":          "INR" if event_name == "transaction_reconciled" else None,
        "amount_band":       None,
        "source_bank":       random.choice(["HDFC","ICICI","SBI","Axis","Kotak"]) if event_name == "transaction_reconciled" else None,
        "source_account_type": None,
        "source_vpa":        None,
        "beneficiary_bank":  None,
        "beneficiary_vpa":   None,
        "merchant_name":     None,
        "merchant_category": None,
        "merchant_category_code": None,
        "transaction_status": _pick(_TXN_STATUSES) if event_name == "transaction_reconciled" else None,
        "settlement_status": None,
        "settlement_type":   None,
        "value_date":        None,
        "network_latency_ms": round(random.uniform(50, 2000), 0) if event_name == "transaction_reconciled" else None,
        "npci_error_code":   None,
        "failure_bank_side": None,
        "is_retriable":      None,
        "jewels_earned":     round(random.uniform(0, 50), 1) if event_name == "transaction_reconciled" else None,
        "is_first_transaction": False,
        "balance_visible":   random.random() < 0.7 if event_name == "app_opened" else None,
        "notification_count": random.randint(0, 5) if event_name == "notification_received" else None,
        "pending_actions":   random.randint(0, 3) if event_name == "app_opened" else None,
        "notification_type": random.choice(["promo","alert","reminder"]) if event_name == "notification_received" else None,
        "campaign_id":       str(uuid.uuid4())[:8] if event_name == "notification_received" else None,
        "is_tapped":         random.random() < 0.3 if event_name == "notification_received" else None,
        "cumulative_jewels": round(random.uniform(0, 500), 1),
        "can_reschedule":    None,
    }
    return row


def seed(db_path: Path = DB_PATH, target_rows: int = 270_000) -> None:
    conn = duckdb.connect(str(db_path))

    # Idempotency check
    existing = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp >= '2026-04-01'"
    ).fetchone()[0]
    if existing >= 100_000:
        print(f"Apr–Jun data already present ({existing:,} rows). Skipping.")
        conn.close()
        return

    print(f"Seeding ~{target_rows:,} events for Apr–Jun 2026…")

    start = datetime(2026, 4, 1)
    end   = datetime(2026, 5, 22, 23, 59, 59)

    # Reuse existing user_ids so retention / cohort metrics work properly
    user_ids = conn.execute(
        "SELECT DISTINCT user_id FROM events LIMIT 15000"
    ).df()["user_id"].tolist()
    conn.close()

    rng = random.Random(42)
    random.seed(42)

    events = list(_EVENT_WEIGHTS.keys())
    weights = list(_EVENT_WEIGHTS.values())

    rows: list[dict] = []
    batch_size = 10_000

    conn = duckdb.connect(str(db_path))
    inserted = 0

    for _ in range(target_rows):
        user_id    = rng.choice(user_ids)
        event_name = random.choices(events, weights=weights, k=1)[0]
        ts         = _rand_ts(start, end)
        rows.append(_make_row(user_id, event_name, ts))

        if len(rows) >= batch_size:
            conn.executemany(
                f"INSERT INTO events VALUES ({','.join(['?'] * len(rows[0]))})",
                [list(r.values()) for r in rows],
            )
            inserted += len(rows)
            rows = []
            print(f"  {inserted:,}/{target_rows:,}", end="\r", flush=True)

    if rows:
        conn.executemany(
            f"INSERT INTO events VALUES ({','.join(['?'] * len(rows[0]))})",
            [list(r.values()) for r in rows],
        )
        inserted += len(rows)

    conn.close()
    print(f"\nDone. Inserted {inserted:,} rows. Total events now covers Apr–May 22 2026.")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--rows", type=int, default=270_000)
    args = p.parse_args()
    seed(Path(args.db), args.rows)
