import sqlite3
import os
import json
import glob

DB_PATH = "data/tartarus.db"
JSON_DIR = "data/word_lists/"

def run_realistic_projection_snapshot():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        print("\n\033[96m" + "="*95 + "\033[0m")
        print("\033[96m                    GERMAN ULTIMATE MASTER SNAPSHOT & PROJECTION                  \033[0m")
        print("\033[96m" + "="*95 + "\033[0m")

        # 1. PARSE ALL JSON FILES FOR TRUE TOTALS
        total_json_vocab = 0
        total_json_sentences = 0
        
        json_files = sorted(glob.glob(os.path.join(JSON_DIR, "bahman_german*.json")))
        for f_path in json_files:
            file_name = os.path.basename(f_path)
            if file_name in ["bahman_german.json", "bahman_german_sentences.json"]:
                continue
            with open(f_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                    count = len(data)
                    if 'sentences' in file_name:
                        total_json_sentences += count
                    else:
                        total_json_vocab += count
                except:
                    pass
                    
        total_true_items = total_json_vocab + total_json_sentences

        # 2. LIFETIME MACRO STATS FROM DB
        cursor.execute("""
            SELECT SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count), SUM(drilled_count)
            FROM sessions_bahman WHERE language LIKE 'german%'
        """)
        lt_dur, lt_reps, lt_corr, lt_inc, lt_drill = cursor.fetchone()
        lt_dur = lt_dur or 0; lt_reps = lt_reps or 0; lt_corr = lt_corr or 0; lt_inc = lt_inc or 0; lt_drill = lt_drill or 0
        acc = (lt_corr / max(1, lt_corr + lt_inc)) * 100

        tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'words_bahman_german%'").fetchall()]
        
        db_vocab_mastered, db_sent_mastered = 0, 0
        deck_counts = {}
        for t in tables:
            stats = conn.execute(f'SELECT COUNT(*), SUM(CASE WHEN score >= 9 THEN 1 ELSE 0 END) FROM "{t}" WHERE active=1').fetchone()
            count = stats[0] or 0
            mastered = stats[1] or 0
            deck_counts[t] = count
            if 'sentences' in t:
                db_sent_mastered += mastered
            else:
                db_vocab_mastered += mastered
                
        total_mastered = db_vocab_mastered + db_sent_mastered
        true_pct = (total_mastered / max(1, total_true_items)) * 100

        print(f"\033[93m[1. LIFETIME PROGRESS VS TRUE CURRICULUM]\033[0m")
        print(f" Total Time     : {lt_dur/3600:.1f} Hours")
        print(f" Total Reps     : {lt_reps:,}")
        print(f" True Accuracy  : {acc:.1f}%")
        print(f" Global Mastery : {total_mastered:,} / {total_true_items:,} items ({true_pct:.2f}% of full 40k curriculum)")
        print(f"  ├─ Vocab      : {db_vocab_mastered:,} / {total_json_vocab:,} words")
        print(f"  └─ Sentences  : {db_sent_mastered:,} / {total_json_sentences:,} sentences")
        print()

        # 3. LAST 7 DAYS VELOCITY
        print(f"\033[93m[2. LAST 7 DAYS VELOCITY]\033[0m")
        cursor.execute("""
            SELECT session_date, SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count), SUM(drilled_count)
            FROM sessions_bahman 
            WHERE language LIKE 'german%' AND date(session_date) >= date('now', '-7 days')
            GROUP BY session_date
            ORDER BY session_date DESC
        """)
        rows = cursor.fetchall()
        seven_day_dur = 0
        if not rows:
            print(" No sessions recorded in the last 7 days.")
        else:
            header_7d = f" {'Date':<12} | {'Time (Min)':<10} | {'Reps':<6} | {'Accuracy':<9} | {'Drills':<6}"
            print(header_7d)
            print("-" * len(header_7d))
            for row in rows:
                s_date, s_dur, s_reps, s_corr, s_inc, s_drill = row
                seven_day_dur += (s_dur or 0)
                s_acc = (s_corr / max(1, s_corr + s_inc)) * 100
                print(f" {s_date:<12} | {s_dur/60:<10.1f} | {s_reps:<6} | {s_acc:<8.1f}% | {s_drill:<6}")
        print()

        # 4. TERMINAL ASCII GRAPH (7-Day Duration & Accuracy)
        print(f"\033[93m[3. 7-DAY VISUAL ACTIVITY (Minutes)]\033[0m")
        if rows:
            max_dur = max([(r[1] or 0) / 60 for r in rows])
            for row in reversed(rows): # Print chronologically
                s_date, s_dur = row[0], (row[1] or 0) / 60
                bar_len = int((s_dur / max_dur) * 40) if max_dur > 0 else 0
                
                # Color code based on accuracy
                s_corr, s_inc = row[3] or 0, row[4] or 0
                s_acc = (s_corr / max(1, s_corr + s_inc)) * 100
                color = "\033[92m" if s_acc >= 90 else ("\033[93m" if s_acc >= 80 else "\033[91m")
                bar = color + "█" * bar_len + "\033[0m"
                
                print(f" {s_date} | {s_dur:>5.1f}m | {bar}")
        print()

        # 5. ACQUISITION PIPELINE
        print(f"\033[93m[4. ACQUISITION PIPELINE (Active SQLite Decks)]\033[0m")
        header = f" {'Deck Name':<22} | {'DB Tot':<6} | {'Unseen':<7} | {'Learn(1-3)':<10} | {'Audio(4-6)':<10} | {'Prod(7-8)':<9} | {'Mastered(9+)':<12}"
        print(header)
        print("-" * len(header))
        for t in tables:
            stats = conn.execute(f'''
                SELECT 
                    SUM(CASE WHEN times_practiced = 0 AND score < 9 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score > 0 AND score < 4 AND times_practiced > 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score >= 4 AND score < 7 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score >= 7 AND score < 9 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score >= 9 THEN 1 ELSE 0 END)
                FROM "{t}" WHERE active=1
            ''').fetchone()
            unseen, b1, b2, b3, mastered = [x or 0 for x in stats]
            name = t.replace('words_bahman_', '')
            print(f" {name:<22} | {deck_counts[t]:<6} | {unseen:<7} | {b1:<10} | {b2:<10} | {b3:<9} | {mastered:<12}")
        print()

        # 6. LEITNER MAINTENANCE SCHEDULE
        print(f"\033[93m[5. LEITNER MAINTENANCE SCHEDULE (Active SQLite Decks)]\033[0m")
        header_leitner = f" {'Deck Name':<22} | {'DB Tot':<6} | {'Box 1(1d)':<9} | {'Box 2(2d)':<9} | {'Box 3(4d)':<9} | {'Box 4(9d)':<9} | {'Box 5(14d)':<9}"
        print(header_leitner)
        print("-" * len(header_leitner))
        for t in tables:
            boxes = conn.execute(f'SELECT leitner_box, COUNT(*) FROM "{t}" WHERE active=1 GROUP BY leitner_box').fetchall()
            d = {b: c for b, c in boxes}
            name = t.replace('words_bahman_', '')
            print(f" {name:<22} | {deck_counts[t]:<6} | {d.get(1,0):<9} | {d.get(2,0):<9} | {d.get(3,0):<9} | {d.get(4,0):<9} | {d.get(5,0):<9}")
        print()

        # 7. FRICTION ITEMS
        print(f"\033[93m[6. HIGHEST FRICTION ITEMS]\033[0m")
        friction_query = " UNION ALL ".join([
            f"SELECT text, (times_drilled + times_incorrect + times_flagged) as friction, score, '{t.replace('words_bahman_', '')}' as deck FROM \"{t}\"" 
            for t in tables
        ])
        cursor.execute(f"SELECT text, friction, score, deck FROM ({friction_query}) WHERE friction > 0 ORDER BY friction DESC LIMIT 5")
        f_header = f" {'Word / Sentence':<35} | {'Frict':<5} | {'Score':<5} | {'Deck'}"
        print(f_header)
        print("-" * len(f_header))
        for row in cursor.fetchall():
            text = (row[0][:32] + "...") if len(row[0]) > 35 else row[0]
            print(f" {text:<35} | {row[1]:<5} | {row[2]:<5} | {row[3]}")
        print()

        # 8. COGNITIVE COMPLETION PROJECTION
        print(f"\033[93m[7. REALISTIC PROJECTION (Actual 7-Day Pace + Acceleration)]\033[0m")
        if lt_dur > 0 and seven_day_dur > 0:
            hours_total = lt_dur / 3600
            current_velocity = total_mastered / hours_total
            avg_daily_hours_7d = (seven_day_dur / 3600) / 7
            
            remaining_vocab = total_json_vocab - db_vocab_mastered
            remaining_sentences = total_json_sentences - db_sent_mastered
            total_remaining = remaining_vocab + remaining_sentences
            
            # The "LEGO" Acceleration Model: 65% faster compounding velocity
            compounding_velocity = current_velocity * 1.65 
            cognitive_hours_left = total_remaining / compounding_velocity
            
            # Scenario: Maintain Current 7-Day Pace
            days_current_pace = cognitive_hours_left / avg_daily_hours_7d if avg_daily_hours_7d > 0 else 0
            
            print(f" Base Velocity         : {current_velocity:.1f} items/hr (Foundational starting rate)")
            print(f" Projected Avg Velocity: ~{compounding_velocity:.1f} items/hr (Applying the 'LEGO' compounding effect)")
            print(f" Remaining Unmastered  : {total_remaining:,} items across all files")
            print(f" Total Active Time Left: ~{cognitive_hours_left:.1f} Hours of typing")
            print(f" -------------------------------------------------------------------------")
            print(f" Actual 7-Day Pace     : {avg_daily_hours_7d:.2f} hours per day")
            print(f" Projected Completion  : ~{int(days_current_pace)} Days to master 40k items (Score 9)")
        else:
            print(" Not enough data to project.")

        print("\n\033[96m" + "="*95 + "\033[0m\n")
        
        conn.close()
    except Exception as e:
        print(f"Diagnostic failed: {e}")

if __name__ == "__main__":
    run_realistic_projection_snapshot()
