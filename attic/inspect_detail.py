
import pandas as pd
import sys

excel_path = sys.argv[1]
df = pd.read_excel(excel_path)

print("=" * 60)
print("UNIQUE VALUES IN KEY COLUMNS")
print("=" * 60)

# Role Categories
print("\nRole Categories:")
role_cats = df["Role Cat"].dropna().unique()
for rc in sorted(role_cats):
    count = len(df[df["Role Cat"] == rc])
    print(f"  • {rc[:50]}... ({count} roles)")

# Status values
print("\nStatus Values:")
statuses = df["Status"].dropna().unique()
for status in sorted(statuses):
    count = len(df[df["Status"] == status])
    print(f"  • {status} ({count} roles)")

# Priority values
print("\nPriority Values:")
priorities = df["Priority"].dropna().unique()
for pri in sorted(priorities):
    count = len(df[df["Priority"] == pri])
    print(f"  • {pri} ({count} roles)")

# Top Orgs
print("\nTop 10 Organizations:")
orgs = df["Org"].value_counts().head(10)
for org, count in orgs.items():
    print(f"  • {org}: {count} roles")

print("\n" + "=" * 60)
print("STATS:")
print("=" * 60)
print(f"Total Roles: {len(df)}")
print(f"Unique Orgs: {df['Org'].nunique()}")
print(f"Open Roles: {len(df[df['Status'].str.contains('Open', na=False)])}")
print(f"Closed Roles: {len(df[df['Status'].str.contains('Closed', na=False)])}")
