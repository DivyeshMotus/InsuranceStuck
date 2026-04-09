import os
import time
import boto3
import requests
import psycopg2
from dotenv import load_dotenv
from io import BytesIO
from twilio.rest import Client
from config import *
import csv
import logging
import builtins

load_dotenv()

print("INSURANCE STUCK STORIES CREATION\n")

DIVYESH_PHONE = os.getenv("DIVYESH_PHONE")
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')

def create_connection():
    params = game_db_config()
    game_db_conn = psycopg2.connect(**params)
    game_db_cur = game_db_conn.cursor()
    return game_db_conn, game_db_cur

def get_insurance_ids_that_entered_stuck(db_cursor):
    query = """
    WITH base AS (
        SELECT
            sf.story_id,
            sf.age_id,
            sf.status,
            sf.created_at,
            s_first.earliest_insurance_update,
            ins_f.prescription,
            ins_f.medical_records,
            ins_f.delivery_ticket,
            cf.subtype,
            CASE
                WHEN sf.age_id IS NULL THEN
                    EXTRACT(EPOCH FROM (NOW() - s_first.earliest_insurance_update)) / 86400
                ELSE
                    EXTRACT(EPOCH FROM (NOW() - a2.begin_time)) / 86400
            END AS insurance_age_in_days,
            CASE
                WHEN sf.age_id IS NULL THEN
                    EXTRACT(EPOCH FROM (NOW() - sf.created_at)) / 86400
                ELSE
                    EXTRACT(EPOCH FROM (NOW() - a3.begin_time)) / 86400
            END AS stuck_age_in_days
        FROM story_fresh AS sf
        LEFT JOIN (
            SELECT DISTINCT ON (story_id)
                story_id,
                age_id,
                MIN(created_at) OVER (PARTITION BY story_id) AS earliest_insurance_update
            FROM story
            WHERE type = 'insurance'
                AND age_id IS NOT NULL
            ORDER BY story_id, created_at ASC
        ) s_first
            ON sf.story_id = s_first.story_id
        LEFT JOIN age_table a2
            ON s_first.age_id = a2.age_id
        LEFT JOIN age_table a3
            ON sf.age_id = a3.age_id
        LEFT JOIN insurance_fresh ins_f
            ON ins_f.insurance_id = sf.story_id
        LEFT JOIN contacts_fresh cf
            ON cf.contact_id = sf.destination
    )
    SELECT story_id FROM base
    WHERE
    (
        (status IN ('insuranceTerminated', 'insuranceVerification', 'needDoctor', 'requestedInfoAdded', 'missingPatientInfo', 'missingProductInfo')
            AND stuck_age_in_days > 7)
        OR
        (status IN ('info', 'readyToBill')
            AND stuck_age_in_days > 5)
        OR
        (status IN ('auth', 'telehealth', 'HMO')
            AND stuck_age_in_days > 14)
        OR
        (status = 'submitted'
            AND stuck_age_in_days > 60)
        OR
        (status = 'newClaim'
            AND stuck_age_in_days > 7)
        or
        (status IN ('auth')
            AND stuck_age_in_days > 30)
        OR
        (status = 'appeal'
            AND stuck_age_in_days > 60)
    )
    and story_id not in (
        select sf.destination
        from story_fresh sf
        where sf.type = 'stuck'
    )
    AND insurance_age_in_days <= 90;
    """
    db_cursor.execute(query)
    rows = db_cursor.fetchall()
    column_names = [desc[0] for desc in db_cursor.description]
    all_rows = [dict(zip(column_names, row)) for row in rows]
    return all_rows

def get_active_operations_specialist(db_cursor):
    query = """SELECT cf.contact_id
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
    row = db_cursor.fetchall()
    contact_id = row[0][0]
    return contact_id

def get_max_story_id(db_cursor):
    query = "SELECT MAX(story_id) FROM story_fresh"
    db_cursor.execute(query)
    row = db_cursor.fetchall()
    max_story_id = row[0][0]
    return max_story_id

def insert_into_age_table(story_id, story_type, db_cursor, db_connection):
    query = """
    INSERT INTO age_table
    (story_id, story_type, status, begin_time)
    VALUES (%s, %s, 'start', NOW())
    RETURNING age_id
    """
    db_cursor.execute(query, (story_id, story_type))
    age_id = db_cursor.fetchone()[0]
    db_connection.commit()
    return age_id

def insert_story_fresh_table(story_id, insurance_id, active_operations_specialist, db_cursor, db_connection):
    query = """
    INSERT INTO story_fresh
        (story_id,
        origin,
        destination,
        type,
        status,
        created_at,
        username)
    VALUES (%s, %s, %s, 'stuck', 'start', NOW(), 'service@motusnova.com')
    """
    db_cursor.execute(query, (story_id, active_operations_specialist, insurance_id))
    db_connection.commit()

def insert_into_story_table(story_id, insurance_id, active_operations_specialist, db_cursor, db_connection):
    query = """
    INSERT INTO story
        (story_id,
        origin,
        destination,
        type,
        status,
        created_at,
        username)
    VALUES (%s, %s, %s, 'stuck', 'start', NOW(), 'service@motusnova.com')
    """
    db_cursor.execute(query, (story_id, active_operations_specialist, insurance_id))
    db_connection.commit()

def update_age_in_story_fresh_table(story_id, age_id, db_cursor, db_connection):
    query = """
    UPDATE story_fresh
    SET created_at = NOW(), 
        age_id = %s
    WHERE story_id = %s
    """
    db_cursor.execute(query, (age_id, story_id,))
    db_connection.commit()

def insert_age_in_story_table(story_id, age_id, db_cursor, db_connection):
    query = """
    INSERT INTO story
        (story_id,
        created_at,
        age_id)
    VALUES (%s, NOW(), %s)
    """
    db_cursor.execute(query, (story_id, age_id))
    db_connection.commit()

def send_completion_sms(insurance_id_list):
    phone_to_send = [DIVYESH_PHONE]
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    message_body = (
        f"Made Stuck stories for insurance IDs - {insurance_id_list}")
    for phone_number in phone_to_send:
        try:
            message = client.messages.create(
                body=message_body,
                from_=TWILIO_PHONE_NUMBER,
                to=phone_number
            )
            print(f"[SMS] Sent to {phone_number}: SID {message.sid}")
        except Exception as e:
            print(f"[SMS] Failed to send to {phone_number}: {e}")

def make_stuck_stories():
    # 1. Connect to DB
    db_connection, db_cursor = create_connection()
    
    # 2. Get the insurance IDs that we will be making stuck stories
    insurance_ids = get_insurance_ids_that_entered_stuck(db_cursor)

    insurance_ids_list = []
    # 3. Iterate through all the insurance IDs
    
    for insurance_id in insurance_ids:
        try:
            # 4. Get random active customer support
            active_operations_specialist = get_active_operations_specialist(db_cursor)
            print(f"Got active customer support id: {active_operations_specialist}")
            
            # 5. Get latest story id
            latest_story_id = get_max_story_id(db_cursor)
            print(f"Latest story ID: {latest_story_id}")

            # 6. Get next story id
            next_story_id = latest_story_id + 1
            print(f"Next story ID: {next_story_id}")

            # 7. Insert into story table
            insert_into_story_table(next_story_id, insurance_id['story_id'], active_operations_specialist, db_cursor, db_connection)
            print("Updated story table")

            # 8. Update story fresh table
            insert_story_fresh_table(next_story_id, insurance_id['story_id'], active_operations_specialist, db_cursor, db_connection)
            print("Updated story fresh table")
            
            # 9. Insert into Age Table and get age_id
            age_id = insert_into_age_table(next_story_id, 'repo', db_cursor, db_connection)
            print(f"Generated age id: {age_id}")

            # 10. Update story fresh table with age_id
            update_age_in_story_fresh_table(next_story_id, age_id, db_cursor, db_connection)
            print("Updated story fresh table with age_id")

            # 11. Insert age_id into story table
            insert_age_in_story_table(next_story_id, age_id, db_cursor, db_connection)
            print("Updated story table with age_id")

            # 12. Append to insurance_id list
            insurance_ids_list.append(insurance_id['story_id'])
            print("Added to list")
        except Exception as e:
            print("❌ Error:", e)
    
    # 13. Close DB connection
    db_cursor.close()
    db_connection.close()
    
    # 14. Send Completion text
    send_completion_sms(insurance_ids_list)

if __name__ == "__main__":
    make_stuck_stories()
    print("\nScript completed")