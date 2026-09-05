import os
import glob
import sys

# Find all .db files in the project (including subfolders)
db_files = []
for root, dirs, files in os.walk('.'):
    for file in files:
        if file.endswith('.db'):
            db_files.append(os.path.join(root, file))

if db_files:
    print("Found database files:")
    for f in db_files:
        print(f"  {f}")
else:
    print("No .db files found.")
    print("Check your database configuration in app/db/database.py")