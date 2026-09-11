
import pandas as pd
from datetime import datetime
import shutil
import sys

excel_path = sys.argv[1]

# Backup the original
backup_path = excel_path.replace(".xlsx", f"-backup-{datetime.now().strftime('%Y%m%d')}.xlsx")
shutil.copy2(excel_path, backup_path)
print(f"✓ Backup created: {backup_path}")

# Read existing data
df = pd.read_excel(excel_path)
print(f"✓ Loaded {len(df)} existing roles")

# Fix typo: Outomes → Outcomes
if 'Outomes' in df.columns:
    df = df.rename(columns={'Outomes': 'Outcomes'})
    print("✓ Fixed: Outomes → Outcomes")

# Add new columns (only if they don't exist)
new_columns = {
    'Source': '',  # Where role was found
    'Date Applied': pd.NaT,  # When you applied
    'Last Updated': datetime.now(),  # Auto-timestamp
    'Match Score': None,  # 0-100% keyword match
    'Keywords Matched': ''  # Which keywords found
}

for col, default_val in new_columns.items():
    if col not in df.columns:
        df[col] = default_val
        print(f"✓ Added column: {col}")
    else:
        print(f"⊘ Column already exists: {col}")

# Reorder columns for better flow
desired_order = [
    'Org', 'Title', 'Role Cat', 'Priority', 
    'Date Opened', 'Date Applied', 'Status', 'Outcomes',
    'Source', 'Match Score', 'Keywords Matched',
    'Role Link', 'Range', 'Notes', 'Other Links', 'Last Updated'
]

# Only reorder columns that exist
existing_order = [col for col in desired_order if col in df.columns]
remaining = [col for col in df.columns if col not in existing_order]
final_order = existing_order + remaining

df = df[final_order]

# Save enhanced file
df.to_excel(excel_path, index=False)
print(f"\n✓ Enhanced tracker saved to: {excel_path}")
print(f"  New columns: {len([c for c in new_columns if c in df.columns])}")
print(f"  Total columns: {len(df.columns)}")
print(f"  Total roles: {len(df)}")
