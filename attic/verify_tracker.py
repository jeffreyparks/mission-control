
import pandas as pd
import sys

excel_path = sys.argv[1]
df = pd.read_excel(excel_path)

print("=" * 60)
print("UPDATED TRACKER VERIFICATION")
print("=" * 60)

print(f"\nTotal roles: {len(df)}")

# Show auto-added roles
auto_added = df[df['Source'] == 'Auto-scan']
print(f"Auto-scanned roles: {len(auto_added)}")

if len(auto_added) > 0:
    print("\nRecently added (sample):")
    print("=" * 60)
    for idx, row in auto_added.head(5).iterrows():
        print(f"\n{row['Org']} - {row['Title']}")
        print(f"  Match Score: {row['Match Score']}/100")
        print(f"  Keywords: {row['Keywords Matched']}")
        print(f"  Status: {row['Status']}")
        print(f"  Link: {row['Role Link'][:50]}...")

print("\n" + "=" * 60)
print("COLUMN VERIFICATION")
print("=" * 60)

required_columns = [
    'Org', 'Title', 'Role Cat', 'Priority', 
    'Date Opened', 'Date Applied', 'Status', 'Outcomes',
    'Source', 'Match Score', 'Keywords Matched',
    'Role Link', 'Range', 'Notes', 'Other Links', 'Last Updated'
]

print(f"\nExpected columns: {len(required_columns)}")
print(f"Actual columns: {len(df.columns)}")

missing = [col for col in required_columns if col not in df.columns]
if missing:
    print(f"\n⚠️  Missing columns: {missing}")
else:
    print("\n✓ All expected columns present")
