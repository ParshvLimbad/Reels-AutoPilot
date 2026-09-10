import sqlite3

c = sqlite3.connect('database/sqlite.db')
print("Configs in DB:")
for row in c.execute('SELECT * FROM configs').fetchall():
    print(row)
