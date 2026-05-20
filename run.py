import os
import csv
import datetime
import logging
import psycopg2
import pandas as pd
from dotenv import load_dotenv
from twilio.rest import Client
from config import *

load_dotenv()


# ---------- Paths ----------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH       = os.path.join(SCRIPT_DIR, "make_stuck_stories.log")
CANDIDATES_CSV = os.path.join(SCRIPT_DIR, "candidates.csv")


# ---------- Logging ----------

def setup_logging():
    """
    Write all output to make_stuck_stories.log next to this script.
    Mode 'w' overwrites on each run. Nothing goes to terminal.
    """
    logging.basicConfig(
        filename=LOG_PATH,
        filemode='w',
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


# ---------- Constants ----------

DIVYESH_PHONE       = os.getenv("DIVYESH_PHONE")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Per-status business-day thresholds. An insurance_id is "stuck" once
# business_days > threshold for its current status.
BUSINESS_DAY_THRESHOLDS = {
    'needDoctor': 7,
    'newClaim': 2,
    'auth': 30,
    'insuranceTerminated': 7,
    'insuranceVerification': 7,
    'info': 1,
    'requestedInfoAdded': 3,
    'readyToBill': 5,
    'submitted': 60,
    'appeal': 60,
    'telehealth': 14,
    'HMO': 14,
    'missingPatientInfo': 2,
    'missingProductInfo': 2,
    'insufficientDiagnosis': 1
}


# ---------- DB connection ----------

def create_connection():
    params = game_db_config()
    conn = psycopg2.connect(**params)
    return conn, conn.cursor()


# ---------- Business-day helper ----------

def calculate_business_days(start_ts):
    """Count business days from start_ts up to today (weekends excluded)."""
    if start_ts is None:
        return 0
    return len(pd.bdate_range(start=start_ts.date(), end=pd.Timestamp.now().date()))


# ---------- Candidate fetch ----------

def get_candidate_insurance_rows(db_cursor):
    query = """
        SELECT
            sf_ins.story_id AS insurance_id,
            sf_ins.status   AS insurance_status,
            CASE
                WHEN sf_ins.age_id IS NULL OR a_ins.status IS DISTINCT FROM sf_ins.status THEN
                    ins.latest_insurance_update
                ELSE
                    a_ins.begin_time
            END AS age_from,
            existing_stuck.stuck_story_id    AS existing_stuck_story_id,
            existing_stuck.stuck_status      AS existing_stuck_status,
            existing_stuck.stuck_notes       AS existing_stuck_notes
        FROM story_fresh AS sf_ins
        LEFT JOIN age_table a_ins
            ON a_ins.age_id = sf_ins.age_id
        LEFT JOIN (
            SELECT
                story_id,
                MAX(created_at) AS latest_insurance_update,
                MIN(created_at) AS earliest_insurance_update
            FROM story
            WHERE status IS NOT NULL
              AND status <> 'duplicate'
            GROUP BY story_id
        ) ins
            ON ins.story_id = sf_ins.story_id
        LEFT JOIN (
            SELECT DISTINCT ON (destination)
                destination        AS insurance_id,
                story_id            AS stuck_story_id,
                status              AS stuck_status,
                notes               AS stuck_notes
            FROM story_fresh
            WHERE type = 'stuck'
            ORDER BY destination, created_at DESC
        ) existing_stuck
            ON existing_stuck.insurance_id = sf_ins.story_id
        LEFT JOIN contacts_fresh cf
        ON cf.contact_id = sf_ins.destination
        WHERE sf_ins.type = 'insurance'
          AND ins.earliest_insurance_update >= '2025-06-01'
    """
    db_cursor.execute(query)
    rows = db_cursor.fetchall()
    columns = [d[0] for d in db_cursor.description]
    return [dict(zip(columns, r)) for r in rows]


def write_candidates_csv(rows):
    """
    Dump the raw candidate query results to candidates.csv next to this
    script. Mode 'w' overwrites the file on every run.
    """
    if not rows:
        # Still overwrite the file so stale data from a previous run is gone.
        open(CANDIDATES_CSV, 'w').close()
        logging.info(f"No candidates fetched; wrote empty {CANDIDATES_CSV}.")
        return

    fieldnames = list(rows[0].keys())
    with open(CANDIDATES_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logging.info(f"Wrote {len(rows)} candidate rows to {CANDIDATES_CSV}.")


def filter_candidates_to_stuck(rows):
    """
    Apply business-day thresholds and the open-stuck-story exclusion.
    Returns two lists: rows needing a fresh stuck story, rows needing a reopen.
    """
    fresh  = []
    reopen = []
    for row in rows:
        status = row['insurance_status']
        threshold = BUSINESS_DAY_THRESHOLDS.get(status)
        if threshold is None:
            continue

        existing_status = row['existing_stuck_status']
        if existing_status is not None and existing_status != 'completed':
            continue

        business_days = calculate_business_days(row['age_from'])
        if business_days <= threshold:
            continue

        if existing_status == 'completed':
            reopen.append(row)
        else:
            fresh.append(row)

    return fresh, reopen


# ---------- Supporting lookups ----------

def get_active_operations_specialist(db_cursor):
    query = """
        SELECT cf.contact_id
        FROM contacts_fresh cf
        JOIN story_fresh sf
          ON sf.destination = cf.contact_id
         AND sf.type = 'employment_status'
         AND sf.status = 'active'
        WHERE cf.type = 'operationsSpecialist'
        ORDER BY RANDOM()
        LIMIT 1;
    """
    db_cursor.execute(query)
    row = db_cursor.fetchone()
    return row[0] if row else None


# ---------- Notes helper ----------

def make_reopen_notes(existing_notes, insurance_status):
    existing_notes = existing_notes or ""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_entry = f"start [{now}] service@motusnova.com: Stuck in {insurance_status}"
    if existing_notes:
        return new_entry + "\n" + existing_notes
    return new_entry


# ---------- Write helpers (no commit — transactions managed by caller) ----------

def insert_into_age_table(story_id, story_type, cursor):
    query = """
        INSERT INTO age_table (story_id, story_type, status, begin_time)
        VALUES (%s, %s, 'start', NOW())
        RETURNING age_id
    """
    cursor.execute(query, (story_id, story_type))
    return cursor.fetchone()[0]


def insert_into_story_fresh_create(insurance_id, ops_specialist, cursor):
    """
    Insert a new stuck story into story_fresh, letting the sequence
    auto-generate story_id. Returns the generated story_id.
    """
    # Advance the sequence to be safe, then let the INSERT claim the next value.
    cursor.execute(
        "SELECT setval('story_fresh_story_id_seq', COALESCE(MAX(story_id), 0) + 1, false) FROM story_fresh;"
    )
    query = """
        INSERT INTO story_fresh
            (origin, destination, type, status, created_at, future_timestamp, username)
        VALUES (%s, %s, 'stuck', 'start', NOW(), NOW(), 'service@motusnova.com')
        RETURNING story_id
    """
    cursor.execute(query, (ops_specialist, insurance_id))
    return cursor.fetchone()[0]

def insert_into_story(next_story_id, insurance_id, ops_specialist, cursor):
    """
    Insert a new stuck story into story_fresh, letting the sequence
    auto-generate story_id. Returns the generated story_id.
    """
    query = """
        INSERT INTO story
            (story_id, origin, destination, type, status, created_at, future_timestamp, username)
        VALUES (%s, %s, %s, 'stuck', 'start', NOW(), NOW(), 'service@motusnova.com')
    """
    cursor.execute(query, (next_story_id, ops_specialist, insurance_id))

def update_age_in_story_fresh(story_id, age_id, cursor):
    query = """
        UPDATE story_fresh
        SET created_at = NOW(),
            username = 'service@motusnova.com',
            age_id = %s
        WHERE story_id = %s
    """
    cursor.execute(query, (age_id, story_id))


def insert_age_in_story(story_id, age_id, cursor):
    query = """
        INSERT INTO story (story_id, created_at, username, age_id)
        VALUES (%s, NOW(), 'service@motusnova.com', %s)
    """
    cursor.execute(query, (story_id, age_id))

def update_age_table(age_id, cursor):
    query = """
        UPDATE age_table
        SET end_time = NOW()
        WHERE age_id = %s
    """
    cursor.execute(query, (age_id, ))

def reopen_story_fresh(story_id, new_notes, cursor):
    """Flip an existing completed stuck story back to 'start' with appended notes."""
    query = """
        UPDATE story_fresh
        SET status = 'start',
            created_at = NOW(),
            future_timestamp = NOW(),
            username = 'service@motusnova.com',
            notes = %s
        WHERE story_id = %s
    """
    cursor.execute(query, (new_notes, story_id))

def insert_reopen_history_row(story_id, new_notes, cursor):
    """Append a 'start' history row to `story` for a reopened stuck story."""
    query = """
        INSERT INTO story
            (story_id, status, notes, created_at, future_timestamp, username)
        VALUES (%s, 'start', %s, NOW(), NOW(), 'service@motusnova.com')
    """
    cursor.execute(query, (story_id, new_notes))

def get_current_age_id(stuck_story_id, cursor):
    query="""
        SELECT age_id FROM story_fresh WHERE story_id = %s;
    """
    cursor.execute(query, (stuck_story_id, ))
    return cursor.fetchone()[0]

# ---------- Per-row orchestration ----------

def create_fresh_stuck_story(row, conn, cursor):
    """Build a brand-new stuck story for an insurance_id that has none."""
    insurance_id = row['insurance_id']
    try:
        ops_specialist = get_active_operations_specialist(cursor)
        next_story_id = insert_into_story_fresh_create(insurance_id, ops_specialist, cursor)
        insert_into_story(next_story_id, insurance_id, ops_specialist, cursor)
        age_id = insert_into_age_table(next_story_id, 'stuck', cursor)
        update_age_in_story_fresh(next_story_id, age_id, cursor)
        insert_age_in_story(next_story_id, age_id, cursor)

        conn.commit()
        logging.info(f"Created fresh stuck story {next_story_id} for insurance_id={insurance_id}")
        return insurance_id
    except Exception:
        conn.rollback()
        logging.exception(f"Failed fresh stuck story for insurance_id={insurance_id}")
        return None


def reopen_stuck_story(row, conn, cursor):
    """Reopen a previously-completed stuck story for the same insurance_id."""
    insurance_id     = row['insurance_id']
    stuck_story_id   = row['existing_stuck_story_id']
    insurance_status = row['insurance_status']
    existing_notes   = row['existing_stuck_notes']

    try:
        new_notes = make_reopen_notes(existing_notes, insurance_status)
        current_age_id = get_current_age_id(stuck_story_id, cursor)
        logging.info(f"Current age id: {current_age_id}")
        reopen_story_fresh(stuck_story_id, new_notes, cursor)
        insert_reopen_history_row(stuck_story_id, new_notes, cursor)
        update_age_table(current_age_id, cursor)
        logging.info("Updated.")
        age_id = insert_into_age_table(stuck_story_id, 'stuck', cursor)
        update_age_in_story_fresh(stuck_story_id, age_id, cursor)
        insert_age_in_story(stuck_story_id, age_id, cursor)

        conn.commit()
        logging.info(f"Reopened stuck story {stuck_story_id} for insurance_id={insurance_id}")
        return insurance_id
    except Exception:
        conn.rollback()
        logging.exception(f"Failed reopen for insurance_id={insurance_id}, stuck_story_id={stuck_story_id}")
        return None


# ---------- SMS ----------

def send_completion_sms(insurance_id_list):
    if not insurance_id_list:
        logging.info("No stuck stories made/reopened; skipping SMS.")
        return
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    body = f"Made Stuck stories for insurance IDs - {insurance_id_list}"
    for phone in [DIVYESH_PHONE]:
        try:
            msg = client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=phone)
            logging.info(f"SMS sent to {phone}: SID {msg.sid}")
        except Exception:
            logging.exception(f"SMS failed to send to {phone}")


# ---------- Main ----------

def make_stuck_stories():
    cursor = None
    conn = None 
    try:
        conn, cursor = create_connection()

        candidates = get_candidate_insurance_rows(cursor)
        logging.info(f"Fetched {len(candidates)} candidate insurance rows from DB.")
        write_candidates_csv(candidates)

        fresh, reopen = filter_candidates_to_stuck(candidates)
        logging.info(f"{len(fresh)} need a fresh stuck story, {len(reopen)} need a reopen.")

        processed_insurance_ids = []

        for row in fresh:
            result = create_fresh_stuck_story(row, conn, cursor)
            if result is not None:
                processed_insurance_ids.append(result)

        for row in reopen:
            result = reopen_stuck_story(row, conn, cursor)
            if result is not None:
                processed_insurance_ids.append(result)

        logging.info(f"Done. {len(processed_insurance_ids)} stuck stories created or reopened.")
        send_completion_sms(processed_insurance_ids)

    except Exception:
        logging.exception("Fatal error in make_stuck_stories")
    finally:
        if cursor is not None:
            try: cursor.close()
            except Exception: pass
        if conn is not None:
            try: conn.close()
            except Exception: pass


if __name__ == "__main__":
    setup_logging()
    logging.info("INSURANCE STUCK STORIES CREATION")
    logging.info(f"Run date: {datetime.date.today()}")
    make_stuck_stories()
    logging.info("Script completed")