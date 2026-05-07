import sqlite3,os
os.makedirs("memory", exist_ok=True)
conn = sqlite3.connect("memory/cards.db")
conn.execute('''CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()
conn.close()
print("DB created successfully")
