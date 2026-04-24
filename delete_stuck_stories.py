import traceback
import psycopg2
from config import *


def create_connection():
    params = ocho_dev_game_db_config()
    conn = psycopg2.connect(**params)
    return conn, conn.cursor()


def delete_stuck_stories():
    conn, cursor = create_connection()
    try:
        # 1. Preview: how many rows does this affect across all three tables?
        cursor.execute("SELECT COUNT(*) FROM story_fresh WHERE type = 'stuck';")
        sf_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM age_table
            WHERE story_id IN (SELECT story_id FROM story_fresh WHERE type = 'stuck');
        """)
        age_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM story
            WHERE story_id IN (SELECT story_id FROM story_fresh WHERE type = 'stuck');
        """)
        story_count = cursor.fetchone()[0]

        print(f"[PREVIEW] Would delete:")
        print(f"  - {age_count} rows from age_table")
        print(f"  - {story_count} rows from story")
        print(f"  - {sf_count} rows from story_fresh")

        if sf_count == 0:
            print("Nothing to delete.")
            return

        # 2. Stage all three DELETEs (children first, parent last).
        cursor.execute("""
            DELETE FROM age_table
            WHERE story_id IN (SELECT story_id FROM story_fresh WHERE type = 'stuck');
        """)
        staged_age = cursor.rowcount

        cursor.execute("""
            DELETE FROM story
            WHERE story_id IN (SELECT story_id FROM story_fresh WHERE type = 'stuck');
        """)
        staged_story = cursor.rowcount

        cursor.execute("DELETE FROM story_fresh WHERE type = 'stuck';")
        staged_sf = cursor.rowcount

        print(f"[STAGED] Deleted (uncommitted):")
        print(f"  - {staged_age} rows from age_table")
        print(f"  - {staged_story} rows from story")
        print(f"  - {staged_sf} rows from story_fresh")

        # 3. Confirm before commit.
        answer = input("Type 'yes' to COMMIT, anything else to ROLLBACK: ").strip().lower()
        if answer == "yes":
            conn.commit()
            print("[DONE] Committed.")
        else:
            conn.rollback()
            print("[ABORTED] Rolled back. No rows were deleted.")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {e}")
        traceback.print_exc()
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    delete_stuck_stories()
    print("Script finished.")